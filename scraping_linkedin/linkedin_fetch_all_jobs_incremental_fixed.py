#!/usr/bin/env python3
"""
LinkedIn Job Intelligence Agent - Fetch Node (v3)
=================================================

Conservative browser-based collection of publicly accessible LinkedIn job
search results. Uses a regular visible Chromium browser (no stealth plugins,
proxy rotation, fingerprint spoofing, or CAPTCHA bypass).

Requirements:
    pip install playwright
    python -m playwright install chromium

Python: 3.11+
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("linkedin_fetcher")


class AccessRestrictionError(RuntimeError):
    """Raised when LinkedIn presents a security or rate-limit challenge."""


class LoginRequiredError(RuntimeError):
    """Raised when a job detail URL redirects to LinkedIn's sign-in wall."""



# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class JobListing:
    """Represents a single extracted job listing."""
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""
    page_number: int = 0
    search_country: str = ""
    description_status: str = "pending"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description": self.description,
            "url": self.url,
            "page_number": self.page_number,
            "search_country": self.search_country,
            "description_status": self.description_status,
        }


@dataclass
class FetchResult:
    """Aggregated result returned by the Fetch Node."""
    jobs: List[JobListing] = field(default_factory=list)
    pages_visited: int = 0
    jobs_extracted: int = 0
    pages_by_country: Dict[str, int] = field(default_factory=dict)
    pages_attempted_by_country: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pages_visited": self.pages_visited,
            "jobs_extracted": self.jobs_extracted,
            "pages_by_country": self.pages_by_country,
            "pages_attempted_by_country": self.pages_attempted_by_country,
            "jobs": [job.to_dict() for job in self.jobs],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class FetchConfig:
    """Centralized configuration for multi-country collection."""

    KEYWORD: str = "Data Engineer"
    BASE_URL: str = "https://www.linkedin.com/jobs/search"
    JOBS_PER_PAGE: int = 25
    MAX_PAGES_PER_COUNTRY: int = 3

    # Broad geographic Europe list. Transcontinental countries are included.
    EUROPE_COUNTRIES: List[str] = [
        "Albania", "Andorra", "Armenia", "Austria", "Azerbaijan", "Belarus",
        "Belgium", "Bosnia and Herzegovina", "Bulgaria", "Croatia", "Cyprus",
        "Czechia", "Denmark", "Estonia", "Finland", "France", "Georgia",
        "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy",
        "Kazakhstan", "Kosovo", "Latvia", "Liechtenstein", "Lithuania",
        "Luxembourg", "Malta", "Moldova", "Monaco", "Montenegro",
        "Netherlands", "North Macedonia", "Norway", "Poland", "Portugal",
        "Romania", "Russia", "San Marino", "Serbia", "Slovakia", "Slovenia",
        "Spain", "Sweden", "Switzerland", "Turkey", "Ukraine",
        "United Kingdom", "Vatican City",
    ]

    # Requested regions plus the European country list above.
    LOCATIONS: List[str] = list(dict.fromkeys([
        "Egypt",
        "United Arab Emirates",
        "Saudi Arabia",
        *EUROPE_COUNTRIES,
        "United States",
    ]))

    # Conservative pacing. These are fixed waits, not randomized evasion.
    SEARCH_PAGE_WAIT_SECONDS: float = 6.0
    BETWEEN_JOB_PAGES_SECONDS: float = 6.0
    BETWEEN_COUNTRIES_SECONDS: float = 20.0
    MAX_SCROLLS_PER_PAGE: int = 8

    PAGE_LOAD_TIMEOUT: int = 60_000
    ELEMENT_TIMEOUT: int = 20_000
    DETAILS_PANEL_TIMEOUT: int = 15_000

    # Use a visible, regular Chromium profile. Sign in manually only if LinkedIn
    # requires it; the script does not attempt to bypass access restrictions.
    HEADLESS: bool = False
    BROWSER_PROFILE_DIR: str = "linkedin_browser_profile"
    WAIT_FOR_MANUAL_LOGIN: bool = True

    OUTPUT_FILE: str = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "linkedin_jobs.json"
    )

    @classmethod
    
    def build_search_url(cls, location: str, start: int = 0) -> str:
        params = {
            "keywords": cls.KEYWORD,
            "location": location,
            "f_TPR": "",
            "f_WT": "",
            "start": start,
            "position": 1,
            "pageNum": 0,
        }
        return f"{cls.BASE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def polite_delay(min_seconds: float, max_seconds: float) -> None:
    """Wait a fixed duration (the upper bound) between page interactions."""
    delay = max_seconds
    logger.debug("Polite wait: %.2f seconds", delay)
    time.sleep(delay)


def check_access_restriction(
    page: Page,
    *,
    authwall_is_restriction: bool = True,
) -> None:
    """Stop on a security/rate-limit challenge; optionally treat authwall as a block.

    Search-result pages use the default (authwall stops the search). For an
    individual job detail page, authwall is classified separately as a
    sign-in-required condition, not automatically as proof of bot detection.
    """
    url = (page.url or "").lower()
    blocked_url_markers = ["checkpoint", "captcha", "challenge", "too-many-requests"]
    if authwall_is_restriction:
        blocked_url_markers.append("authwall")
    if any(marker in url for marker in blocked_url_markers):
        raise AccessRestrictionError(
            f"LinkedIn access restriction detected at URL: {page.url}"
        )

    try:
        body_text = page.locator("body").inner_text(timeout=2_500).lower()
    except Exception:
        body_text = ""

    blocked_text_markers = (
        "unusual activity",
        "temporarily blocked",
        "verify your identity",
        "security verification",
        "complete a security verification",
        "too many requests",
        "access to this page has been restricted",
    )
    for marker in blocked_text_markers:
        if marker in body_text:
            raise AccessRestrictionError(
                f"LinkedIn displayed a security/rate-limit notice: '{marker}'. "
                "Collection stopped without attempting to bypass it."
            )




# ---------------------------------------------------------------------------
# Overlay & Modal Handling
# ---------------------------------------------------------------------------

def dismiss_overlays(page: Page) -> None:
    """
    Dismiss common LinkedIn overlays before interacting with the page.
    """
    logger.info("Checking for and dismissing overlays...")

    cookie_selectors = [
        'button[action-type="accept"]',
        'button[data-control-name="cookie.policy_banner.accept"]',
        'button[aria-label*="Accept"]',
        'button[aria-label*="Agree"]',
        'button:has-text("Accept")',
        'button:has-text("Agree")',
        'button:has-text("Got it")',
        'button:has-text("Dismiss")',
    ]

    for sel in cookie_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info("Found cookie/consent button: %s", sel)
                btn.click(force=True, delay=100)
                polite_delay(0.5, 1.0)
                logger.info("Cookie/consent button dismissed.")
        except Exception as exc:
            logger.debug("Cookie selector %s failed: %s", sel, exc)

    modal_close_selectors = [
        'button[aria-label="Dismiss"]',
        'button.modal__dismiss',
        'button[data-test-modal-close-btn]',
        'svg[data-test-icon="close-small"]',
        'li-icon[type="cancel-icon"]',
        'button:has-text("Close")',
    ]

    for sel in modal_close_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info("Found modal close button: %s", sel)
                btn.click(force=True, delay=100)
                polite_delay(0.5, 1.0)
                logger.info("Modal dismissed.")
        except Exception as exc:
            logger.debug("Modal close selector %s failed: %s", sel, exc)

    js_hide_overlays = """
    () => {
        const selectors = [
            '.modal__overlay',
            '.top-level-modal-container',
            '.artdeco-modal-overlay',
            '.cookie-policy-banner',
            '#artdeco-global-alert-container',
            '.msg-overlay-list-bubble',
        ];
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                el.style.display = 'none';
                el.style.visibility = 'hidden';
                el.style.pointerEvents = 'none';
            });
        });
        return 'overlays hidden';
    }
    """
    try:
        page.evaluate(js_hide_overlays)
        logger.info("JavaScript overlay cleanup executed.")
    except Exception as exc:
        logger.debug("JS overlay cleanup failed: %s", exc)

    polite_delay(0.5, 1.0)


# ---------------------------------------------------------------------------
# Core Scraping Functions
# ---------------------------------------------------------------------------

def scroll_page_naturally(page: Page, scrolls: int = 3) -> None:
    logger.info("Scrolling page naturally (%d scrolls)...", scrolls)
    for i in range(scrolls):
        scroll_by = 600
        page.evaluate(f"window.scrollBy(0, {scroll_by})")
        polite_delay(0.5, 1.5)
        logger.debug("Scroll %d/%d completed (by %d px)", i + 1, scrolls, scroll_by)


def detect_job_cards(page: Page) -> List[Any]:
    """Detect all job cards on the current search results page."""
    selectors = [
        'li.jobs-search-results__list-item',
        'div.job-card-container',
        '[data-test-id="job-card"]',
        'div.base-card',
        'div.base-card.relative',
    ]

    for selector in selectors:
        cards = page.query_selector_all(selector)
        if cards:
            logger.info("Detected %d job cards using selector: %s", len(cards), selector)
            return cards

    logger.warning("No job cards detected with any known selector.")
    return []


def extract_from_job_card(card: Any) -> Dict[str, str]:
    """Extract listing fields directly from one search-result card."""
    data = {"title": "", "company": "", "location": "", "url": ""}

    def first_text(selectors: List[str]) -> str:
        for selector in selectors:
            try:
                element = card.query_selector(selector)
                if element:
                    value = (element.inner_text() or element.text_content() or "").strip()
                    if value:
                        return " ".join(value.split())
            except Exception:
                continue
        return ""

    data["title"] = first_text([
        ".job-card-list__title",
        ".base-search-card__title",
        ".artdeco-entity-lockup__title",
        'h3[data-test-job-title]',
        "h3",
    ])

    data["company"] = first_text([
        ".job-card-container__primary-description",
        ".base-search-card__subtitle",
        ".artdeco-entity-lockup__subtitle",
        "h4",
    ])

    data["location"] = first_text([
        ".job-card-container__metadata-item",
        ".job-search-card__location",
        ".base-search-card__metadata",
        ".artdeco-entity-lockup__caption",
    ])

    # Prefer the actual job-view URL, not a company/profile link.
    for selector in [
        'a.base-card__full-link',
        'a[href*="/jobs/view/"]',
        'a[href*="/jobs/collections/"]',
    ]:
        try:
            link = card.query_selector(selector)
            if link:
                href = link.get_attribute("href")
                if href:
                    data["url"] = urljoin("https://www.linkedin.com", href).split("?")[0]
                    break
        except Exception:
            continue

    return data


def collect_jobs_from_current_page(
    page: Page,
    page_number: int,
    search_country: str,
    max_scrolls: int = FetchConfig.MAX_SCROLLS_PER_PAGE,
) -> List[JobListing]:
    """Collect every job card encountered while scrolling the results list."""
    collected: List[JobListing] = []
    seen = set()
    stable_rounds = 0

    for scroll_number in range(max_scrolls + 1):
        cards = detect_job_cards(page)
        before = len(collected)

        for card in cards:
            data = extract_from_job_card(card)
            if not data["title"]:
                continue

            # Use URL as the primary key; fall back to the visible listing fields.
            key = data["url"] or "|".join([
                data["title"].casefold(),
                data["company"].casefold(),
                data["location"].casefold(),
            ])
            if key in seen:
                continue

            seen.add(key)
            collected.append(JobListing(
                title=data["title"],
                company=data["company"],
                location=data["location"],
                description="",  # Search cards normally do not contain full descriptions.
                url=data["url"],
                page_number=page_number,
                search_country=search_country,
            ))

        if len(collected) == before:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if scroll_number == max_scrolls or stable_rounds >= 3:
            break

        # LinkedIn often scrolls the results pane rather than the browser window.
        page.evaluate("""() => {
            const cardSelector = 'li.jobs-search-results__list-item, div.job-card-container, [data-test-id="job-card"], div.base-card';
            const firstCard = document.querySelector(cardSelector);
            let node = firstCard ? firstCard.parentElement : null;
            let scrollContainer = null;

            while (node && node !== document.body) {
                const style = window.getComputedStyle(node);
                const canScroll = node.scrollHeight > node.clientHeight + 20;
                const scrollsY = /(auto|scroll)/.test(style.overflowY);
                if (canScroll && scrollsY) {
                    scrollContainer = node;
                    break;
                }
                node = node.parentElement;
            }

            if (scrollContainer) {
                scrollContainer.scrollBy(0, Math.max(350, scrollContainer.clientHeight * 0.8));
            } else {
                window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));
            }
        }""")
        polite_delay(0.8, 1.4)

    logger.info("Collected %d unique job cards from page %d", len(collected), page_number)
    return collected

# ---------------------------------------------------------------------------
# Full job-description extraction
# ---------------------------------------------------------------------------

DESCRIPTION_SELECTORS = [
    'div#job-details',
    'div.jobs-description__content',
    'div.jobs-description-content__text',
    'div.show-more-less-html__markup',
    'div.description__text',
    '[data-test-id="job-description"]',
    '[class*="description__text"]',
]


def expand_job_description(page: Page) -> None:
    """Click the description's Show more / See more control when present."""
    try:
        page.evaluate(r"""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            for (const button of buttons) {
                const label = (button.innerText || button.getAttribute('aria-label') || '')
                    .trim().toLowerCase();
                const isExpandButton = /^(show more|see more|show full description)$/.test(label);
                const belongsToDescription =
                    button.matches('.show-more-less-html__button') ||
                    Boolean(button.closest(
                        '#job-details, .jobs-description__content, '
                        '.show-more-less-html, [class*="description"]'
                    ));
                if (isExpandButton && belongsToDescription) {
                    button.click();
                }
            }
        }""")
        page.wait_for_timeout(300)
    except Exception as exc:
        logger.debug("Could not expand job description: %s", exc)


def extract_job_description(page: Page) -> str:
    """Return the longest non-empty text from known job-description containers."""
    candidates: List[str] = []

    for selector in DESCRIPTION_SELECTORS:
        try:
            for element in page.query_selector_all(selector):
                text = (element.inner_text() or element.text_content() or "").strip()
                text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
                if len(text) > 80:
                    candidates.append(text)
        except Exception:
            continue

    if not candidates:
        # Fallback for LinkedIn markup changes: inspect only elements whose class
        # names indicate a description, rather than grabbing the whole page body.
        try:
            fallback = page.evaluate(r"""() => {
                const nodes = Array.from(document.querySelectorAll(
                    '[class*="description"], [id*="description"]'
                ));
                const texts = nodes
                    .map(node => (node.innerText || '').trim())
                    .filter(text => text.length > 80);
                texts.sort((a, b) => b.length - a.length);
                return texts[0] || '';
            }""")
            if fallback:
                candidates.append(fallback.strip())
        except Exception as exc:
            logger.debug("Description JavaScript fallback failed: %s", exc)

    if not candidates:
        return ""

    # A wrapper may contain the same description plus its heading/buttons. The
    # longest matching description container is generally the most complete.
    description = max(candidates, key=len)
    description = description.replace("Show less", "").strip()
    return "\n".join(line.rstrip() for line in description.splitlines()).strip()


def fetch_job_description(page: Page, job: JobListing) -> str:
    """Open one job URL in the existing tab and extract its description.

    Reusing the search tab avoids depending on a second tab that can be closed
    independently. The next search-page iteration navigates back to search.
    """
    if not job.url:
        logger.warning("No job URL for '%s'; description was skipped.", job.title)
        return ""

    try:
        if page.is_closed():
            raise RuntimeError("The Chromium tab was closed before description extraction.")

        page.goto(
            job.url,
            timeout=FetchConfig.PAGE_LOAD_TIMEOUT,
            wait_until="domcontentloaded",
        )
        if "authwall" in (page.url or "").lower():
            raise LoginRequiredError(
                f"Job detail page requires LinkedIn sign-in: {page.url}"
            )
        check_access_restriction(page, authwall_is_restriction=False)

        try:
            page.wait_for_selector(
                ", ".join(DESCRIPTION_SELECTORS),
                timeout=FetchConfig.DETAILS_PANEL_TIMEOUT,
            )
        except PlaywrightTimeoutError:
            # Some public job pages render the description later or use a new
            # class name, so still attempt the fallback extraction below.
            logger.debug("Known description selector did not appear for %s", job.url)

        page.wait_for_timeout(900)
        expand_job_description(page)
        description = extract_job_description(page)

        if description:
            logger.info(
                "Extracted description for '%s' (%d characters)",
                job.title,
                len(description),
            )
        else:
            logger.warning("Description not found for '%s' (%s)", job.title, job.url)

        if "authwall" in (page.url or "").lower():
            raise LoginRequiredError(
                f"Job detail page requires LinkedIn sign-in: {page.url}"
            )
        check_access_restriction(page, authwall_is_restriction=False)
        return description

    except (AccessRestrictionError, LoginRequiredError):
        raise
    except Exception as exc:
        if page.is_closed():
            raise RuntimeError(
                "The Chromium tab/context closed during description extraction. "
                "The run has to stop; reopen the browser and rerun from the saved checkpoint."
            ) from exc
        logger.warning("Could not fetch description for '%s': %s", job.title, exc)
        return ""

def click_first_job_card(page: Page, cards: List[Any]) -> bool:
    """
    Click the first job card with multiple fallback strategies.
    """
    if not cards:
        logger.warning("No job cards available to click.")
        return False

    first_card = cards[0]
    logger.info("Attempting to click the first job card...")

    # Strategy 1: Normal click
    try:
        first_card.scroll_into_view_if_needed()
        polite_delay(0.3, 0.8)
        first_card.click(delay=100)
        polite_delay(1.0, 2.0)
        logger.info("First job card clicked (normal click).")
        return True
    except Exception as exc:
        logger.warning("Normal click failed: %s", exc)

    # Strategy 2: Force click
    try:
        first_card.scroll_into_view_if_needed()
        polite_delay(0.3, 0.8)
        first_card.click(force=True, delay=100)
        polite_delay(1.0, 2.0)
        logger.info("First job card clicked (force click).")
        return True
    except Exception as exc:
        logger.warning("Force click failed: %s", exc)

    # Strategy 3: JavaScript click
    try:
        first_card.evaluate("element => element.click()")
        polite_delay(1.0, 2.0)
        logger.info("First job card clicked (JavaScript click).")
        return True
    except Exception as exc:
        logger.error("JavaScript click also failed: %s", exc)

    logger.error("All click strategies failed for the first job card.")
    return False


def _extract_text_with_selectors(page: Page, selectors: List[str]) -> str:
    """Helper: try multiple selectors and return the first non-empty text."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.text_content() or "").strip()
                if text:
                    logger.debug("Selector matched: %s -> '%s...'", sel, text[:50])
                    return text
        except Exception:
            continue
    return ""


def extract_job_details(page: Page, page_number: int, card_fallback: Optional[Dict[str, str]] = None) -> Optional[JobListing]:
    """
    Extract structured data from the job details panel or full job view page.

    LinkedIn has two layouts for public users:
      1. Split-panel: search results on left, job details on right (same URL)
      2. Full-page: clicking navigates to /jobs/view/... page

    We try selectors for both layouts, plus JavaScript fallback.
    """
    job = JobListing(page_number=page_number)
    job.url = page.url

    # --- Title ---
    # Layout 1: Logged-in / panel view selectors
    # Layout 2: Public / full-page view selectors
    title_selectors = [
        # Panel view (logged-in style)
        'h1.job-details-jobs-unified-top-card__job-title',
        'h2.job-details-jobs-unified-top-card__job-title',
        # Full-page public view
        'h1.top-card-layout__title',
        'h2.top-card-layout__title',
        '.topcard__title',
        'h1.t-24',
        '[data-test-id="job-title"]',
        'h1',
        'h2',
    ]
    job.title = _extract_text_with_selectors(page, title_selectors)

    # --- Company ---
    company_selectors = [
        'a.job-details-jobs-unified-top-card__company-name',
        'span.job-card-container__primary-description',
        '.top-card-layout__entity-info a',
        '.topcard__org-name-link',
        '[data-test-id="company-name"]',
        '.top-card-layout__card a.subtitle',
    ]
    job.company = _extract_text_with_selectors(page, company_selectors)

    # --- Location ---
    location_selectors = [
        'span.job-details-jobs-unified-top-card__bullet',
        'span.tvm__text--low-emphasis',
        '.topcard__flavor--bullet',
        '.top-card-layout__entity-info span',
        '[data-test-id="job-location"]',
        'span.topcard__flavor',
    ]
    job.location = _extract_text_with_selectors(page, location_selectors)

    # --- Description ---
    description_selectors = [
        'div#job-details',
        'div.jobs-description__content',
        'div.job-details-jobs-unified-top-card__description',
        'div.show-more-less-html__markup',
        'div.description__text',
        '[data-test-id="job-description"]',
        'article',
    ]
    job.description = _extract_text_with_selectors(page, description_selectors)

    # --- JavaScript fallback for title/company if still empty ---
    # Only run JS fallback if we genuinely have empty fields AND no card fallback
    # This prevents overwriting good card data with bad JS guesses
    needs_js = False
    if not job.title and (not card_fallback or not card_fallback.get("title")):
        needs_js = True
    if not job.company and (not card_fallback or not card_fallback.get("company")):
        needs_js = True
    if not job.location and (not card_fallback or not card_fallback.get("location")):
        needs_js = True
    if not job.description:
        needs_js = True

    if needs_js:
        logger.info("Attempting JavaScript fallback extraction...")
        js_extract = r"""
        () => {
            const result = { title: "", company: "", location: "", description: "" };

            // Try to find title from h1 or h2
            const h1 = document.querySelector('h1');
            const h2 = document.querySelector('h2');
            if (h1) result.title = h1.innerText.trim();
            else if (h2) result.title = h2.innerText.trim();

            // Try company from anchor tags containing /company/ (avoid using title as company)
            const companyLinks = Array.from(document.querySelectorAll('a[href*="/company/"]'));
            for (const link of companyLinks) {
                const text = link.innerText.trim();
                // Don't accept if it looks like a job title (too long or contains "Engineer", "Manager", etc.)
                if (text && text.length < 60 && !text.match(/Engineer|Manager|Developer|Analyst|Designer|Architect/i)) {
                    result.company = text;
                    break;
                }
            }

            // Try location from spans that look like "City, Country" or "City, State"
            const spans = Array.from(document.querySelectorAll('span'));
            for (const span of spans) {
                const text = span.innerText.trim();
                if (text.match(/^[A-Za-zÀ-ÿ\s]+,\s*[A-Za-zÀ-ÿ\s]+$/)) {
                    result.location = text;
                    break;
                }
            }

            // Try description from large text blocks
            const divs = Array.from(document.querySelectorAll('div'));
            for (const div of divs) {
                if (div.innerText.length > 500 && div.children.length > 3) {
                    result.description = div.innerText.trim().substring(0, 2000);
                    break;
                }
            }

            return result;
        }
        """
        try:
            js_result = page.evaluate(js_extract)
            if js_result:
                if not job.title and js_result.get("title"):
                    job.title = js_result["title"]
                if not job.company and js_result.get("company"):
                    job.company = js_result["company"]
                if not job.location and js_result.get("location"):
                    job.location = js_result["location"]
                if not job.description and js_result.get("description"):
                    job.description = js_result["description"]
        except Exception as exc:
            logger.debug("JavaScript fallback extraction failed: %s", exc)

    # --- Apply card fallback for missing fields ---
    # Also use card fallback to CORRECT bad extractions (e.g., company = title)
    if card_fallback:
        if not job.title and card_fallback.get("title"):
            job.title = card_fallback["title"]
        # Fix: if company equals title, it's probably wrong — use card fallback
        if (not job.company or job.company == job.title) and card_fallback.get("company"):
            job.company = card_fallback["company"]
        if not job.location and card_fallback.get("location"):
            job.location = card_fallback["location"]
        # Prefer direct job URL from card over search page URL
        if card_fallback.get("url"):
            job.url = card_fallback["url"]

    # Validation
    if not job.title:
        logger.warning("Extraction returned empty title after all attempts.")
        return None

    logger.info(
        "Extracted job: title='%s' company='%s' location='%s'",
        job.title, job.company, job.location,
    )
    return job


def has_next_page(page: Page, current_start: int, collected_count: int = 0) -> bool:
    """Detect whether a 'Next' pagination button exists and is enabled."""
    next_selectors = [
        'button[aria-label="Next"]',
        'button.artdeco-pagination__button--next',
        'li[data-test-pagination-page-btn].active + li',
        'a[aria-label="Next"]',
    ]

    for sel in next_selectors:
        btn = page.query_selector(sel)
        if btn:
            disabled = btn.get_attribute("disabled")
            aria_disabled = btn.get_attribute("aria-disabled")
            if disabled or aria_disabled == "true":
                logger.info("Next button found but disabled (last page).")
                return False
            logger.info("Next page button detected and enabled.")
            return True

    cards = detect_job_cards(page)
    # Use the number collected while scrolling as well, because LinkedIn may
    # virtualize the list and keep only a subset of cards in the DOM.
    visible_or_collected = max(len(cards), collected_count)
    if visible_or_collected < FetchConfig.JOBS_PER_PAGE:
        logger.info(
            "Only %d jobs collected (< %d per page). Assuming last page.",
            visible_or_collected, FetchConfig.JOBS_PER_PAGE,
        )
        return False

    if visible_or_collected >= FetchConfig.JOBS_PER_PAGE:
        logger.info(
            "No explicit next button found, but the batch is full; "
            "the next offset page will be tested (subject to the per-country cap)."
        )
        return True

    logger.info("No explicit next button found and the batch is short; assuming last page.")
    return False


def click_next_page(page: Page) -> bool:
    """Click the 'Next' pagination button with fallback strategies."""
    next_selectors = [
        'button[aria-label="Next"]',
        'button.artdeco-pagination__button--next',
        'a[aria-label="Next"]',
    ]

    for sel in next_selectors:
        btn = page.query_selector(sel)
        if btn:
            try:
                btn.scroll_into_view_if_needed()
                polite_delay(0.3, 0.8)
                btn.click(force=True, delay=100)
                logger.info("Clicked next page button (force click).")
                return True
            except Exception as exc:
                logger.error("Failed to click next button (%s): %s", sel, exc)
                try:
                    btn.evaluate("element => element.click()")
                    logger.info("Clicked next page button (JavaScript).")
                    return True
                except Exception as js_exc:
                    logger.error("JS click fallback also failed: %s", js_exc)

    logger.warning("Could not locate or click any next page button.")
    return False


# ---------------------------------------------------------------------------
# Retry Wrapper
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Main Fetch Node
# ---------------------------------------------------------------------------

def save_results_checkpoint(result: FetchResult, output_path: str) -> None:
    """Write a complete, valid JSON snapshot after each job is processed.

    JSON arrays/objects cannot be safely extended by simply adding bytes at the
    end while keeping the document valid. Therefore this writes a temporary
    complete JSON document and atomically replaces the previous checkpoint.
    """
    temporary_path = f"{output_path}.tmp"
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    with open(temporary_path, "w", encoding="utf-8") as output_file:
        json.dump(result.to_dict(), output_file, indent=2, ensure_ascii=False)
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, output_path)


def load_existing_results(input_path: str) -> FetchResult:
    """Load a previous checkpoint so a rerun adds to it instead of overwriting it."""
    if not os.path.exists(input_path):
        return FetchResult()

    try:
        with open(input_path, "r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read existing checkpoint %s; starting a new result set: %s",
            input_path, exc,
        )
        return FetchResult()

    jobs: List[JobListing] = []
    for item in payload.get("jobs", []):
        if not isinstance(item, dict):
            continue
        jobs.append(JobListing(
            title=str(item.get("title", "")),
            company=str(item.get("company", "")),
            location=str(item.get("location", "")),
            description=str(item.get("description", "")),
            url=str(item.get("url", "")),
            page_number=int(item.get("page_number", 0) or 0),
            search_country=str(item.get("search_country", "")),
            description_status=str(item.get("description_status", "pending")),
        ))

    return FetchResult(
        jobs=jobs,
        pages_visited=int(payload.get("pages_visited", 0) or 0),
        jobs_extracted=len(jobs),
        pages_by_country={
            str(key): int(value) for key, value in payload.get("pages_by_country", {}).items()
        },
        pages_attempted_by_country={
            str(key): int(value)
            for key, value in payload.get("pages_attempted_by_country", {}).items()
        },
        errors=[str(error) for error in payload.get("errors", [])],
    )


def job_dedupe_key(job: JobListing) -> str:
    """Use the URL when available, otherwise the visible listing fields."""
    return job.url or "|".join([
        job.title.casefold(),
        job.company.casefold(),
        job.location.casefold(),
    ])


def fetch_linkedin_jobs() -> FetchResult:
    """Search each configured location, attempting up to three offset pages.

    Page offsets are requested directly (0, 25, 50). We deliberately do not
    depend on LinkedIn's visible "Next" control because some search layouts do
    not expose it consistently. Duplicate jobs caused by overlapping offsets
    or scrolling are removed by canonical URL.
    """
    result = load_existing_results(FetchConfig.OUTPUT_FILE)
    all_seen = {job_dedupe_key(job) for job in result.jobs}
    stop_all = False
    # Set to True after a job detail page redirects to authwall. Search-result
    # pages can still be collected, but individual description URLs are skipped.
    description_access_blocked = False

    logger.info("=" * 70)
    logger.info("Starting multi-country LinkedIn job collection")
    logger.info("Keyword: %s", FetchConfig.KEYWORD)
    logger.info("Countries/locations: %d", len(FetchConfig.LOCATIONS))
    logger.info("Maximum pages per country: %d", FetchConfig.MAX_PAGES_PER_COUNTRY)
    logger.info("Page offset increment: %d", FetchConfig.JOBS_PER_PAGE)
    logger.info("JSON checkpoint: %s", FetchConfig.OUTPUT_FILE)
    logger.info("Loaded existing jobs from checkpoint: %d", len(result.jobs))
    logger.info("=" * 70)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=FetchConfig.BROWSER_PROFILE_DIR,
            headless=FetchConfig.HEADLESS,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        # One regular browser tab is reused for search and job details.
        page = context.new_page()

        if FetchConfig.WAIT_FOR_MANUAL_LOGIN:
            page.goto(
                "https://www.linkedin.com/",
                timeout=FetchConfig.PAGE_LOAD_TIMEOUT,
                wait_until="domcontentloaded",
            )
            print(
                "\nA regular Chromium window is open. Sign in manually only if needed. "
                "Do not automate or bypass any security verification."
            )
            input("Press Enter in this terminal to start the country searches...")

        try:
            for country_index, country in enumerate(FetchConfig.LOCATIONS, start=1):
                logger.info(
                    "========== Location %d/%d: %s =========",
                    country_index, len(FetchConfig.LOCATIONS), country,
                )
                current_start = 0
                pages_for_country = result.pages_by_country.get(country, 0)

                for page_number in range(1, FetchConfig.MAX_PAGES_PER_COUNTRY + 1):
                    result.pages_attempted_by_country[country] = max(
                        result.pages_attempted_by_country.get(country, 0), page_number
                    )
                    save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)

                    search_url = FetchConfig.build_search_url(
                        location=country,
                        start=current_start,
                    )
                    logger.info(
                        "--- %s | page %d/%d | start=%d ---",
                        country, page_number, FetchConfig.MAX_PAGES_PER_COUNTRY,
                        current_start,
                    )

                    try:
                        page.goto(
                            search_url,
                            timeout=FetchConfig.PAGE_LOAD_TIMEOUT,
                            wait_until="domcontentloaded",
                        )
                        check_access_restriction(page)
                        polite_delay(
                            FetchConfig.SEARCH_PAGE_WAIT_SECONDS,
                            FetchConfig.SEARCH_PAGE_WAIT_SECONDS,
                        )
                        dismiss_overlays(page)

                        try:
                            page.wait_for_selector(
                                'li.jobs-search-results__list-item, '
                                'div.job-card-container, div.base-card, '
                                '[data-test-id="job-card"]',
                                timeout=FetchConfig.ELEMENT_TIMEOUT,
                            )
                        except PlaywrightTimeoutError:
                            check_access_restriction(page)
                            logger.info(
                                "No job cards appeared for %s page %d (start=%d). "
                                "No further results were available at this offset.",
                                country, page_number, current_start,
                            )
                            result.errors.append(
                                f"{country}, page {page_number}: no job cards appeared "
                                f"at start={current_start}."
                            )
                            break

                        dismiss_overlays(page)
                        page_jobs = collect_jobs_from_current_page(
                            page=page,
                            page_number=page_number,
                            search_country=country,
                            max_scrolls=FetchConfig.MAX_SCROLLS_PER_PAGE,
                        )
                        check_access_restriction(page)

                        if not page_jobs:
                            logger.info(
                                "No jobs collected for %s page %d (start=%d); "
                                "stopping this location.",
                                country, page_number, current_start,
                            )
                            break

                        # Deduplicate globally by canonical job URL. If LinkedIn
                        # overlaps results between offsets, only one copy is kept.
                        new_page_jobs: List[JobListing] = []
                        duplicate_count = 0
                        for job in page_jobs:
                            key = job_dedupe_key(job)
                            if key not in all_seen:
                                all_seen.add(key)
                                new_page_jobs.append(job)
                            else:
                                duplicate_count += 1

                        pages_for_country += 1
                        result.pages_by_country[country] = pages_for_country
                        result.pages_visited += 1
                        save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)

                        logger.info(
                            "%s page %d: %d cards collected, %d new jobs, "
                            "%d duplicates skipped.",
                            country, page_number, len(page_jobs),
                            len(new_page_jobs), duplicate_count,
                        )

                        # First persist metadata for EVERY card on this search page.
                        # Do this before opening any job detail URL: a detail-page
                        # authwall must not prevent the other cards from being saved.
                        for job_index, job in enumerate(new_page_jobs, start=1):
                            result.jobs.append(job)
                            result.jobs_extracted = len(result.jobs)
                            save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                            logger.info(
                                "Saved listing metadata %d/%d for %s: %s",
                                job_index, len(new_page_jobs), country, job.title,
                            )

                        # Only attempt descriptions while detail pages are publicly
                        # accessible. If LinkedIn redirects one to authwall, record
                        # that sign-in is required and stop further detail-page
                        # requests, but keep collecting search-card metadata.
                        description_page_restricted = False
                        for job_index, job in enumerate(new_page_jobs, start=1):
                            if description_access_blocked:
                                job.description_status = "login_required_for_details"
                                save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                                continue

                            logger.info(
                                "Description %d/%d | %s | %s",
                                job_index, len(new_page_jobs), country, job.title,
                            )
                            try:
                                job.description = fetch_job_description(page, job)
                                job.description_status = (
                                    "extracted" if job.description else "not_found"
                                )
                            except LoginRequiredError as exc:
                                job.description_status = "login_required"
                                description_access_blocked = True
                                logger.warning(
                                    "LinkedIn requires sign-in for job details. "
                                    "Will skip further description requests but continue "
                                    "collecting accessible search-result cards. %s",
                                    exc,
                                )
                            except AccessRestrictionError as exc:
                                job.description_status = "access_restricted"
                                result.errors.append(str(exc))
                                logger.error(
                                    "Security/rate-limit challenge on a job detail page; "
                                    "stopping further browsing: %s", exc,
                                )
                                stop_all = True
                                description_page_restricted = True
                            except Exception as exc:
                                job.description_status = "error"
                                result.errors.append(
                                    f"Description error for '{job.title}': {exc}"
                                )
                                logger.warning(
                                    "Description extraction failed for '%s': %s",
                                    job.title, exc,
                                )

                            # Save after each individual description result/status.
                            save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                            logger.info(
                                "JSON checkpoint updated: %s | description=%d chars | status=%s",
                                FetchConfig.OUTPUT_FILE,
                                len(job.description),
                                job.description_status,
                            )

                            if stop_all:
                                break
                            if not description_access_blocked:
                                polite_delay(
                                    FetchConfig.BETWEEN_JOB_PAGES_SECONDS,
                                    FetchConfig.BETWEEN_JOB_PAGES_SECONDS,
                                )

                        if stop_all or description_page_restricted:
                            save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                            break

                        logger.info(
                            "%s page %d completed. Total unique jobs saved: %d",
                            country, page_number, result.jobs_extracted,
                        )
                        save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)

                        # Do not stop merely because the visible Next button is
                        # absent/disabled or the cards are virtualized. Try the
                        # next documented offset, then stop if that URL has no cards.
                        if page_number < FetchConfig.MAX_PAGES_PER_COUNTRY:
                            current_start += FetchConfig.JOBS_PER_PAGE
                            logger.info(
                                "Attempting next results page for %s: page %d, start=%d. "
                                "This is a direct offset request; no Next-button check is used.",
                                country, page_number + 1, current_start,
                            )

                    except AccessRestrictionError as exc:
                        result.errors.append(str(exc))
                        logger.error("Stopping all locations: %s", exc)
                        stop_all = True
                        save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                        break
                    except Exception as exc:
                        logger.exception(
                            "Error while processing %s page %d (start=%d): %s",
                            country, page_number, current_start, exc,
                        )
                        result.errors.append(
                            f"{country}, page {page_number}, start={current_start}: {exc}"
                        )
                        if page.is_closed():
                            logger.error(
                                "The browser tab is closed; stopping the remaining locations."
                            )
                            stop_all = True
                            save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                            break
                        # A genuine navigation/extraction error ends this location's
                        # current page loop, but the JSON checkpoint is preserved.
                        break

                result.pages_by_country[country] = pages_for_country
                save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
                logger.info(
                    "Country summary: %s | pages attempted=%d | pages with results=%d | "
                    "unique jobs currently attributed to country=%d",
                    country,
                    result.pages_attempted_by_country.get(country, 0),
                    pages_for_country,
                    sum(1 for item in result.jobs if item.search_country == country),
                )

                if stop_all:
                    break
                if country_index < len(FetchConfig.LOCATIONS):
                    polite_delay(
                        FetchConfig.BETWEEN_COUNTRIES_SECONDS,
                        FetchConfig.BETWEEN_COUNTRIES_SECONDS,
                    )

        finally:
            context.close()

    save_results_checkpoint(result, FetchConfig.OUTPUT_FILE)
    logger.info("=" * 70)
    logger.info("Collection completed")
    logger.info("Search pages visited: %d", result.pages_visited)
    logger.info("Unique jobs extracted: %d", result.jobs_extracted)
    logger.info("Countries with results: %d", sum(1 for count in result.pages_by_country.values() if count))
    logger.info("Pages attempted by country: %s", result.pages_attempted_by_country)
    logger.info("Saved results to: %s", FetchConfig.OUTPUT_FILE)
    if result.errors:
        logger.info("Errors/restrictions recorded: %d", len(result.errors))
    logger.info("=" * 70)

    return result


# ---------------------------------------------------------------------------
# Pretty Printer
# ---------------------------------------------------------------------------

def print_results(result: FetchResult) -> None:
    """Print extracted jobs in a readable format."""
    print("\n" + "=" * 70)
    print("LINKEDIN JOB FETCH RESULTS")
    print("=" * 70)
    print(f"Visited Pages:   {result.pages_visited}")
    print(f"Extracted Jobs:  {result.jobs_extracted}")
    print("-" * 70)

    for idx, job in enumerate(result.jobs, 1):
        print(f"\n--- Job {idx} (Page {job.page_number}) ---")
        print(f"  Title:       {job.title}")
        print(f"  Company:     {job.company}")
        print(f"  Location:    {job.location}")
        print(f"  Searched in: {job.search_country}")
        print(f"  URL:         {job.url}")
        print(f"  Description status: {job.description_status}")
        if job.description:
            desc_preview = job.description.replace("\n", " ")[:300]
            if len(job.description) > 300:
                desc_preview += "..."
            print(f"  Description: {desc_preview}")

    if result.errors:
        print("\n--- Errors ---")
        for err in result.errors:
            print(f"  • {err}")

    print("\n" + "=" * 70)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fetch_result = fetch_linkedin_jobs()
    print_results(fetch_result)
    print(
        f"\nSaved {fetch_result.jobs_extracted} unique jobs to: "
        f"{FetchConfig.OUTPUT_FILE}"
    )
