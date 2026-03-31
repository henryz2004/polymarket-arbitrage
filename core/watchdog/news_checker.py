"""
News Checker
==============

Fetches recent news headlines from Google News RSS to determine
whether a price spike has a public catalyst.

No API key required — uses public RSS feed + stdlib XML parsing.

Relevance scoring: headlines must share at least MIN_KEYWORD_OVERLAP
non-stopword keywords with the event title to count as "relevant."
This prevents broad keyword matches (e.g. "brazil" + "election")
from marking every tangential article as a catalyst.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree

import httpx

from core.watchdog.models import WatchdogConfig

logger = logging.getLogger(__name__)

# Common stopwords to filter from search queries
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "must", "of", "in", "to",
    "for", "with", "on", "at", "from", "by", "about", "as", "into",
    "through", "during", "before", "after", "above", "below", "between",
    "out", "off", "over", "under", "again", "further", "then", "once",
    "and", "but", "or", "nor", "not", "no", "so", "if", "than", "too",
    "very", "just", "how", "what", "which", "who", "whom", "this", "that",
    "these", "those", "it", "its", "us", "we", "they", "them", "their",
    "our", "your", "his", "her", "yes", "market", "markets", "price",
    "first", "new", "latest", "says", "report", "news", "update",
}

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Minimum number of event-title keywords that must appear in a headline
# for it to count as a relevant catalyst (prevents loose matches).
MIN_KEYWORD_OVERLAP = 2


class NewsChecker:
    """Fetches recent news headlines matching event keywords."""

    def __init__(self, config: WatchdogConfig):
        self.config = config
        self._http_client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """Initialize HTTP client."""
        self._http_client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WatchdogBot/1.0)",
            },
        )

    async def stop(self) -> None:
        """Cleanup HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    async def fetch_headlines(self, event_title: str) -> list[str]:
        """
        Fetch recent news headlines related to an event title.

        Only returns headlines that are *relevant* to the event — i.e.,
        share at least MIN_KEYWORD_OVERLAP meaningful keywords with the
        event title. This prevents broad matches from inflating
        news_driven classifications.

        Args:
            event_title: The market event title to search for.

        Returns:
            List of relevant headline strings from the last news_lookback_hours.
        """
        if not self._http_client:
            return []

        keywords = self._extract_keywords(event_title)
        if not keywords:
            return []

        query = " ".join(keywords[:5])  # Limit to 5 keywords for focused search

        try:
            resp = await self._http_client.get(
                GOOGLE_NEWS_RSS,
                params={
                    "q": query,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
            )

            if resp.status_code != 200:
                logger.debug(f"Google News RSS returned {resp.status_code} for query: {query}")
                return []

            all_headlines = self._parse_rss(resp.text)

            # Filter to relevant headlines only
            keyword_set = set(keywords)
            relevant = []
            for headline in all_headlines:
                if self._headline_relevance(headline, keyword_set) >= MIN_KEYWORD_OVERLAP:
                    relevant.append(headline)

            return relevant

        except Exception as e:
            logger.debug(f"News fetch error for '{query}': {e}")
            return []

    def _headline_relevance(self, headline: str, event_keywords: set[str]) -> int:
        """
        Count how many event keywords appear in a headline.

        Returns the overlap count. Higher = more relevant.
        """
        headline_words = set(re.findall(r'[a-zA-Z]+', headline.lower()))
        # Remove stopwords from headline for cleaner matching
        headline_meaningful = headline_words - STOPWORDS
        return len(event_keywords & headline_meaningful)

    def _extract_keywords(self, event_title: str) -> list[str]:
        """
        Extract meaningful keywords from an event title.

        Filters stopwords and short words, prioritizes watch_keywords matches.
        """
        # Clean and split
        words = re.findall(r'[a-zA-Z]+', event_title.lower())

        # Filter stopwords and short words
        meaningful = [w for w in words if w not in STOPWORDS and len(w) > 2]

        # Prioritize watch_keywords — put matches first
        watch_set = set(k.lower() for k in self.config.watch_keywords)
        priority = [w for w in meaningful if w in watch_set]
        rest = [w for w in meaningful if w not in watch_set]

        return priority + rest

    def _parse_rss(self, xml_text: str) -> list[str]:
        """
        Parse Google News RSS XML and return recent headlines.

        Filters to articles published within news_lookback_hours.
        """
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as e:
            logger.debug(f"RSS XML parse error: {e}")
            return []

        headlines = []
        cutoff = datetime.utcnow() - timedelta(hours=self.config.news_lookback_hours)

        # RSS structure: rss > channel > item
        channel = root.find("channel")
        if channel is None:
            return []

        for item in channel.findall("item"):
            title_elem = item.find("title")
            pub_date_elem = item.find("pubDate")

            if title_elem is None or title_elem.text is None:
                continue

            headline = title_elem.text.strip()

            # Filter by publication date if available
            if pub_date_elem is not None and pub_date_elem.text:
                pub_date = self._parse_rss_date(pub_date_elem.text)
                if pub_date and pub_date < cutoff:
                    continue

            headlines.append(headline)

        return headlines[:10]  # Cap at 10 headlines

    def _parse_rss_date(self, date_str: str) -> Optional[datetime]:
        """Parse RSS pubDate format (RFC 822) and return naive UTC datetime."""
        # Common formats: "Wed, 27 Feb 2026 10:15:00 GMT"
        formats = [
            "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M:%S %z",
            "%d %b %Y %H:%M:%S %Z",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                # Convert timezone-aware datetimes to UTC before stripping tzinfo.
                # Without this, "+0500" timestamps are stored as naive with the
                # wrong hour, making headlines appear more recent than they are.
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except ValueError:
                continue
        return None
