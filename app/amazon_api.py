import os
import requests

RAINFOREST_API_KEY = os.getenv("RAINFOREST_API_KEY")

def search_amazon_products(query: str) -> list[dict]:
    """Search Amazon for real products using Rainforest API and return top DP links."""
    try:
        url = "https://api.rainforestapi.com/request"
        params = {
            "api_key": RAINFOREST_API_KEY,
            "type": "search",
            "amazon_domain": "amazon.com",
            "search_term": query,
            "page": 1,
            "include_sponsored": "false"
        }

        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("search_results", []):
            asin = item.get("asin")
            if not asin:
                continue
            results.append({
                "name": item.get("title"),
                "url": f"https://www.amazon.com/dp/{asin}?tag=schizobestie-20",
                "category": "auto",  # override in intent if needed
                "review": item.get("snippet") or "Well-rated and loved — exactly what you're looking for."
            })

        return results

    except Exception as e:
        print(f"[Rainforest API] Error: {e}")
        return []
