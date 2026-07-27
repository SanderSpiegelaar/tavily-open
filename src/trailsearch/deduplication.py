"""
Lightweight semantic deduplication using SimHash algorithm.

SimHash is a locality-sensitive hashing algorithm that maps similar content
to similar hash values, enabling fast near-duplicate detection without
vector embeddings or GPU requirements.
"""

from __future__ import annotations

import hashlib
import re


class SimHash:
    """SimHash implementation for content similarity detection."""

    def __init__(self, hash_bits: int = 64):
        """
        Initialize SimHash with specified bit length.

        Args:
            hash_bits: Number of bits for hash (default: 64)
        """
        self.hash_bits = hash_bits

    def _tokenize(self, text: str) -> list[str]:
        """
        Tokenize text into words.

        Args:
            text: Input text to tokenize

        Returns:
            List of lowercase word tokens
        """
        # Remove special characters and split into words
        text = re.sub(r"[^\w\s]", " ", text.lower())
        return [word for word in text.split() if len(word) > 2]

    def _hash_token(self, token: str) -> int:
        """
        Hash a single token to integer.

        Args:
            token: Word token to hash

        Returns:
            Integer hash value
        """
        return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)

    def compute(self, text: str) -> int:
        """
        Compute SimHash fingerprint for text.

        Args:
            text: Input text to hash

        Returns:
            Integer SimHash fingerprint
        """
        if not text or not text.strip():
            return 0

        tokens = self._tokenize(text)
        if not tokens:
            return 0

        # Initialize bit vector with zeros
        vector = [0] * self.hash_bits

        # Process each token
        for token in tokens:
            token_hash = self._hash_token(token)

            # Update vector based on token hash bits
            for i in range(self.hash_bits):
                if token_hash & (1 << i):
                    vector[i] += 1
                else:
                    vector[i] -= 1

        # Generate final fingerprint from vector
        fingerprint = 0
        for i in range(self.hash_bits):
            if vector[i] > 0:
                fingerprint |= 1 << i

        return fingerprint

    def hamming_distance(self, hash1: int, hash2: int) -> int:
        """
        Calculate Hamming distance between two hashes.

        Args:
            hash1: First hash value
            hash2: Second hash value

        Returns:
            Number of differing bits
        """
        xor = hash1 ^ hash2
        distance = 0
        while xor:
            distance += 1
            xor &= xor - 1  # Remove rightmost 1-bit
        return distance

    def similarity(self, hash1: int, hash2: int) -> float:
        """
        Calculate similarity score between two hashes.

        Args:
            hash1: First hash value
            hash2: Second hash value

        Returns:
            Similarity score between 0.0 and 1.0
        """
        distance = self.hamming_distance(hash1, hash2)
        return 1.0 - (distance / self.hash_bits)


def deduplicate_by_content(
    items: list[dict],
    content_key: str = "content",
    similarity_threshold: float = 0.85,
) -> list[dict]:
    """
    Deduplicate items based on content similarity using SimHash.

    Args:
        items: List of items to deduplicate (must have content_key field)
        content_key: Key name for content field in items
        similarity_threshold: Minimum similarity to consider duplicates (0.0-1.0)

    Returns:
        Deduplicated list of items
    """
    if not items:
        return []

    simhash = SimHash(hash_bits=64)
    unique_items = []
    seen_hashes = []

    for item in items:
        content = item.get(content_key, "")
        if not content:
            # Keep items without content
            unique_items.append(item)
            continue

        # Compute SimHash for this item
        item_hash = simhash.compute(content)

        # Check similarity with existing items
        is_duplicate = False
        for seen_hash in seen_hashes:
            similarity = simhash.similarity(item_hash, seen_hash)
            if similarity >= similarity_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            unique_items.append(item)
            seen_hashes.append(item_hash)

    return unique_items


def deduplicate_by_url(items: list[dict], url_key: str = "url") -> list[dict]:
    """
    Deduplicate items by exact URL match.

    Args:
        items: List of items to deduplicate (must have url_key field)
        url_key: Key name for URL field in items

    Returns:
        Deduplicated list of items
    """
    if not items:
        return []

    seen_urls = set()
    unique_items = []

    for item in items:
        url = item.get(url_key, "")
        if not url:
            unique_items.append(item)
            continue

        # Normalize URL for comparison
        normalized_url = url.lower().rstrip("/")

        if normalized_url not in seen_urls:
            unique_items.append(item)
            seen_urls.add(normalized_url)

    return unique_items


def deduplicate_search_results(
    results: list[dict],
    url_dedup: bool = True,
    content_dedup: bool = True,
    similarity_threshold: float = 0.85,
) -> list[dict]:
    """
    Deduplicate search results using URL and/or content similarity.

    Args:
        results: List of search result dictionaries
        url_dedup: Enable URL-based deduplication
        content_dedup: Enable content-based deduplication
        similarity_threshold: Similarity threshold for content dedup (0.0-1.0)

    Returns:
        Deduplicated search results
    """
    if not results:
        return []

    deduplicated = results

    # First pass: URL deduplication (faster, catches exact duplicates)
    if url_dedup:
        deduplicated = deduplicate_by_url(deduplicated, url_key="url")

    # Second pass: Content deduplication (catches near-duplicates)
    if content_dedup:
        deduplicated = deduplicate_by_content(
            deduplicated,
            content_key="content",
            similarity_threshold=similarity_threshold,
        )

    return deduplicated
