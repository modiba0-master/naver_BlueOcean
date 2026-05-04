() => {
                                        const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
                                        const text = (el) => (el ? norm(el.textContent) : "");
                                        const pickPrice = (card) => {
                                            // 1) custom-oos 대표 판매가 우선
                                            const cands = card.querySelectorAll(
                                                ".custom-oos span, .custom-oos div, [class*='custom-oos'] span"
                                            );
                                            for (const n of cands) {
                                                const t = norm(n.innerText || n.textContent || "");
                                                if (!t || t.includes("개당")) continue;
                                                const mm = t.match(/[\d,]+\s*원/);
                                                if (mm) return norm(mm[0].replace(/\s+/g, ""));
                                            }
                                            // 2) fallback
                                            const area = card.querySelector(".PriceArea_priceArea__NntJz")
                                                || card.querySelector(".sale-price")
                                                || card.querySelector("[class*='price']");
                                            if (!area) return "";
                                            const blob = norm(area.innerText || area.textContent || "");
                                            const re = /[\d,]+\s*원/g;
                                            let first = "";
                                            let m;
                                            while ((m = re.exec(blob)) !== null) {
                                                if (!first) first = m[0];
                                            }
                                            return first ? norm(first.replace(/\s+/g, "")) : "";
                                        };
                                        const pickShippingKeywords = (blob) => {
                                            if (!blob) return "";
                                            const kws = [
                                                "로켓배송", "판매자로켓", "로켓직구", "로켓그로스", "새벽배송",
                                                "오늘 출발", "오늘출발", "도착보장", "내일도착", "내일 도착",
                                                "무료배송", "판매자 배송", "판매자배송",
                                            ];
                                            const seen = [];
                                            for (let i = 0; i < kws.length; i++) {
                                                const kw = kws[i];
                                                if (blob.includes(kw) && seen.indexOf(kw) === -1) seen.push(kw);
                                            }
                                            return seen.join(" / ");
                                        };
                                        const pickShipping = (card) => {
                                            const fee = card.querySelector(
                                                ".TextBadge_feePrice__n_gta, [data-badge-type='feePrice']"
                                            );
                                            if (fee) return norm(fee.textContent);
                                            const trySels = [
                                                "[class*='DeliveryInfo']",
                                                "[class*='deliveryInfo']",
                                                "[class*='DeliveryBadge']",
                                                "[class*='RocketBadge']",
                                                "[class*='RocketDelivery']",
                                                "[class*='rocketDelivery']",
                                                "[class*='ProductUnit_badge']",
                                                "[class*='ImageBadge']",
                                                "[class*='BadgeList']",
                                            ];
                                            for (let i = 0; i < trySels.length; i++) {
                                                const n = card.querySelector(trySels[i]);
                                                if (n) {
                                                    const t = norm(n.textContent);
                                                    if (t && !/^\d+%$/.test(t)) return t;
                                                }
                                            }
                                            const badgeBlob = Array.from(card.querySelectorAll(
                                                "[class*='Badge'], [class*='badge'], [class*='Delivery'], "
                                                + "[class*='delivery'], [class*='Label'], [class*='label'], "
                                                + "[class*='Rocket'], [class*='rocket'], [data-badge-type]"
                                            )).map((n) => norm(n.textContent)).join(" ");
                                            let kw = pickShippingKeywords(badgeBlob);
                                            if (kw) return kw;
                                            kw = pickShippingKeywords(norm(card.innerText || card.textContent || ""));
                                            return kw;
                                        };
                                        const pickReviewScore = (card) => {
                                            const wrap = card.querySelector(".ProductRating_productRating__jjf7W");
                                            if (!wrap) return "";
                                            const labeled = wrap.querySelector("[aria-label]");
                                            if (labeled) {
                                                const al = norm(labeled.getAttribute("aria-label") || "");
                                                const am = al.match(/(\d+(?:\.\d+)?)/);
                                                if (am) return am[1];
                                            }
                                            const starSels = ["em", "strong", "[class*='rating']"];
                                            for (let si = 0; si < starSels.length; si++) {
                                                const n = wrap.querySelector(starSels[si]);
                                                if (n) {
                                                    const t = norm(n.textContent);
                                                    const tm = t.match(/(\d+(?:\.\d+)?)/);
                                                    if (tm) return tm[1];
                                                }
                                            }
                                            return "";
                                        };
                                        const pickProductUrl = (card) => {
                                            let a = card.querySelector(
                                                "a[href*='vp/products'], a[href*='/products/'], "
                                                + "a[href*='www.coupang.com/vp/'], a[href^='/vp/products']"
                                            );
                                            if (!a) a = card.querySelector("a[href]");
                                            if (!a) return "";
                                            let href = (a.getAttribute("href") || "").trim();
                                            if (!href) return "";
                                            if (href.startsWith("/")) href = "https://www.coupang.com" + href;
                                            return href;
                                        };
                                        const pickReview = (card) => {
                                            const el = card.querySelector(
                                                ".ProductRating_productRating__jjf7W [class*='fw-text-'], "
                                                + ".rating-total-count, .rating-count, .count"
                                            );
                                            const t = el ? norm(el.textContent) : "";
                                            const paren = t.match(/\(\s*([\d,]+)\s*\)/);
                                            if (paren) return paren[1].replace(/,/g, "");
                                            const digits = t.match(/[\d,]+/);
                                            return digits ? digits[0].replace(/,/g, "") : t;
                                        };
                                        const cards = Array.from(document.querySelectorAll(
                                            "li.ProductUnit_productUnit__Qd6sv, li.search-product, "
                                            + "ul#product-list > li, ul#productList li, li[data-product-id], "
                                            + "li[class*='ProductUnit'], li[class*='productUnit']"
                                        ));
                                        const isAd = (card) => {
                                            if (card.querySelector(
                                                ".search-product__ad-badge, .search-product__ad, .ad-badge-text"
                                            )) return true;
                                            if (norm(card.textContent).includes("광고")
                                                && !card.querySelector("[class*='RankMark_rank']")) return true;
                                            return false;
                                        };
                                        const extract = (card) => {
                                            const titleEl = card.querySelector(
                                                ".ProductUnit_productNameV2__cV9cw, .name, "
                                                + "a[class*='productName'], [class*='productName'], "
                                                + "dd.descriptions a, .product-name"
                                            );
                                            return {
                                                title: text(titleEl),
                                                price: pickPrice(card),
                                                shipping: pickShipping(card),
                                                review_count: pickReview(card),
                                                review_score: pickReviewScore(card),
                                                url: pickProductUrl(card),
                                            };
                                        };
                                        const rankFromCard = (card) => {
                                            const nodes = card.querySelectorAll("[class*='RankMark_rank']");
                                            for (let ri = 0; ri < nodes.length; ri++) {
                                                const el = nodes[ri];
                                                const cls = el.className || "";
                                                const blob = typeof cls === "string" ? cls : String(cls || "");
                                                const mWhole = blob.match(/RankMark_rank(\d+)/);
                                                if (mWhole) {
                                                    const rv = parseInt(mWhole[1], 10);
                                                    if (rv >= 1 && rv <= 10) return rv;
                                                }
                                                const parts = blob.split(/\s+/);
                                                for (let pj = 0; pj < parts.length; pj++) {
                                                    const mm = parts[pj].match(/^RankMark_rank(\d+)/);
                                                    if (mm) {
                                                        const rv2 = parseInt(mm[1], 10);
                                                        if (rv2 >= 1 && rv2 <= 10) return rv2;
                                                    }
                                                }
                                                const t = norm(el.textContent || "");
                                                if (/^\d{1,2}$/.test(t)) {
                                                    const rv3 = parseInt(t, 10);
                                                    if (rv3 >= 1 && rv3 <= 10) return rv3;
                                                }
                                            }
                                            return null;
                                        };
                                        const byRank = {};
                                        const seenUrlsRank = new Set();
                                        for (const card of cards) {
                                            const rk = rankFromCard(card);
                                            if (rk === null) continue;
                                            if (isAd(card)) continue;
                                            const row = extract(card);
                                            const tn = norm(row.title);
                                            if (tn.length < 4) continue;
                                            const u = norm(row.url);
                                            const dedupeKey = rk + "|" + u;
                                            if (u && seenUrlsRank.has(dedupeKey)) continue;
                                            if (u) seenUrlsRank.add(dedupeKey);
                                            if (!(rk in byRank)) {
                                                byRank[rk] = {
                                                    rank: rk,
                                                    title: row.title,
                                                    price: row.price,
                                                    shipping: row.shipping,
                                                    review_count: row.review_count,
                                                    review_score: row.review_score,
                                                    url: row.url,
                                                };
                                            }
                                        }
                                        let top10 = [];
                                        for (let r = 1; r <= 10; r++) {
                                            if (byRank[r]) top10.push(byRank[r]);
                                        }
                                        let organic_count = Object.keys(byRank).length;
                                        if (organic_count === 0) {
                                            const organicRows = [];
                                            const seenUrls = new Set();
                                            for (const card of cards) {
                                                if (isAd(card)) continue;
                                                const row = extract(card);
                                                const tn = norm(row.title);
                                                if (tn.length < 4) continue;
                                                const u = norm(row.url);
                                                if (u && seenUrls.has(u)) continue;
                                                if (u) seenUrls.add(u);
                                                organicRows.push(row);
                                            }
                                            organic_count = organicRows.length;
                                            top10 = organicRows.slice(0, 10).map((row, idx) => ({
                                                rank: idx + 1,
                                                title: row.title,
                                                price: row.price,
                                                shipping: row.shipping,
                                                review_count: row.review_count,
                                                review_score: row.review_score,
                                                url: row.url,
                                            }));
                                        }
                                        const sample = top10.slice(0, 5).map((row) => ({
                                            name: row.title,
                                            price: row.price,
                                            review_score: row.review_score,
                                            url: row.url,
                                        }));
                                        const html = document.documentElement && document.documentElement.outerHTML;
                                        const nextDataProbe = (() => {
                                            const el = document.getElementById("__NEXT_DATA__");
                                            const winHas =
                                                typeof window.__NEXT_DATA__ !== "undefined" &&
                                                window.__NEXT_DATA__ !== null;
                                            if (!el || !el.textContent) {
                                                return {
                                                    script_present: false,
                                                    window_present: winHas,
                                                };
                                            }
                                            try {
                                                const j = JSON.parse(el.textContent);
                                                const rootKeys =
                                                    j && typeof j === "object"
                                                        ? Object.keys(j).slice(0, 24)
                                                        : [];
                                                let propsKeys = [];
                                                if (j && j.props && typeof j.props === "object") {
                                                    propsKeys = Object.keys(j.props).slice(0, 24);
                                                }
                                                return {
                                                    script_present: true,
                                                    parse_ok: true,
                                                    root_keys: rootKeys,
                                                    props_keys: propsKeys,
                                                    window_present: winHas,
                                                };
                                            } catch (e) {
                                                return {
                                                    script_present: true,
                                                    parse_ok: false,
                                                    parse_err: String(e),
                                                    window_present: winHas,
                                                };
                                            }
                                        })();
                                        return {
                                            url: location.href,
                                            title: document.title || "",
                                            html_len: html ? html.length : 0,
                                            card_count: cards.length,
                                            organic_count: organic_count,
                                            top10: top10,
                                            sample: sample,
                                            next_data_probe: nextDataProbe,
                                            _probe_rev: "rankmark-nextdata-probe-20260203",
                                        };
                                    }