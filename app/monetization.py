def choose(candidates):
    # candidates: dicts with url, commission_pct, sponsor_bid_cents, last_ctr, last_conv_rate
    def score(c):
        ctr = c.get("last_ctr", 0.0)
        conv = c.get("last_conv_rate", 0.0)
        return c.get("commission_pct", 0.0) * 0.5 + (c.get("sponsor_bid_cents", 0)/100.0) * 0.4 + (ctr+conv) * 0.1
    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0]
