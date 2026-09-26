import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
SEED = 20260924
CATEGORIES = ["GROCERY", "FUEL", "ELECTRONICS", "TRAVEL", "APPAREL", "PHARMA", "RESTAURANT"]
CHANNELS = ["POS", "ONLINE", "QR"]
RISKS = ["LOW", "MEDIUM", "HIGH"]


def merchant_rows():
    rows = []
    for i in range(30):
        mid = f"M{1000 + i}"
        name = f"{random.choice(['ABC', 'Sunrise', 'Metro', 'Global', 'Vertex', 'Shakti', 'Nimbus'])} {random.choice(['Retail', 'Traders', 'Stores', 'Mart', 'Services'])} {i}"
        cat = random.choice(CATEGORIES)
        if i % 4 == 0:
            rows.append([mid, name, cat, "IN", "LOW", "2026-01-01", "2026-03-31"])
            rows.append([mid, name, cat, "IN", "HIGH", "2026-04-01", "2026-06-30"])
            rows.append([mid, name, cat, "IN", "MEDIUM", "2026-07-01", ""])
        elif i % 7 == 0:
            rows.append([mid, name, cat, "IN", "MEDIUM", "2026-01-01", "2026-08-31"])
            rows.append([mid, name, cat, "IN", "HIGH", "2026-09-01", ""])
        else:
            rows.append([mid, name, cat, "IN", random.choice(RISKS), "2026-01-01", ""])
    return rows


def build(days, start, txn_seq, evt_seq, stl_seq, merchants, problem):
    txns, stls, evts = [], [], []
    bad_merchants = ["M9999", "M8888"]
    for d in range(days):
        day = start + timedelta(days=d)
        for _ in range(random.randint(700, 900)):
            txn_seq += 1
            tid = f"T{txn_seq}"
            mid = random.choice(merchants)
            roll = random.random()
            if roll < 0.004:
                mid = ""
            elif roll < 0.008:
                mid = random.choice(bad_merchants)
            ts = day + timedelta(
                hours=random.randint(6, 22), minutes=random.randint(0, 59), seconds=random.randint(0, 59)
            )
            amount = round(random.choice([random.uniform(150, 4000), random.uniform(4000, 60000), random.uniform(60000, 400000)]), 2)
            status = random.choices(["SUCCESS", "FAILED", "REVERSED"], weights=[0.90, 0.07, 0.03])[0]
            currency = "INR"
            cr = random.random()
            if cr < 0.002:
                currency = "IN"
            elif cr < 0.004:
                currency = ""
            elif cr < 0.006:
                currency = "USD"
            txns.append([tid, mid, f"C{random.randint(50000, 99999)}", ts.isoformat(sep=" "), f"{amount:.2f}", currency, status, random.choice(CHANNELS)])

            evt_seq += 1
            created = ts - timedelta(seconds=random.randint(5, 40))
            evts.append([f"E{evt_seq}", tid, "CREATED", created, created + timedelta(seconds=random.randint(1, 20)), random.randint(20, 400)])
            if status in ("SUCCESS", "REVERSED"):
                evt_seq += 1
                evts.append([f"E{evt_seq}", tid, "AUTHORIZED", ts, ts + timedelta(seconds=random.randint(1, 30)), random.randint(20, 500)])
            else:
                evt_seq += 1
                evts.append([f"E{evt_seq}", tid, "FAILED", ts, ts + timedelta(seconds=random.randint(1, 30)), random.randint(20, 500)])

            if status != "SUCCESS":
                continue
            bad = mid in problem
            if random.random() < (0.11 if bad else 0.015):
                continue
            delay = random.choices(
                [random.randint(2, 28), random.randint(31, 180), random.randint(200, 1500)],
                weights=[0.65, 0.25, 0.10] if bad else [0.97, 0.02, 0.01],
            )[0]
            settled_ts = ts + timedelta(minutes=delay)
            split = random.random() < 0.08
            parts = [amount] if not split else [round(amount * 0.8, 2), round(amount - round(amount * 0.8, 2), 2)]
            for idx, part in enumerate(parts):
                stl_seq += 1
                sid = f"S{stl_seq}"
                sstat = "SETTLED"
                sr = random.random()
                if sr < (0.09 if bad else 0.012):
                    sstat = "PENDING"
                elif sr < (0.13 if bad else 0.02):
                    sstat = "FAILED"
                amt = part
                if random.random() < 0.004:
                    amt = -abs(part)
                p_ts = settled_ts + timedelta(minutes=idx * random.randint(0, 12))
                stls.append([sid, tid, p_ts.isoformat(sep=" "), f"{amt:.2f}", sstat, f"B{p_ts.strftime('%Y%m%d')}-{random.randint(1, 6)}"])
                if sstat == "SETTLED":
                    evt_seq += 1
                    ing = p_ts + timedelta(seconds=random.randint(1, 45))
                    if random.random() < 0.06:
                        ing = p_ts + timedelta(minutes=random.randint(6, 90))
                    evts.append([f"E{evt_seq}", tid, "SETTLED", p_ts, ing, random.randint(30, 900)])

        for _ in range(4):
            stl_seq += 1
            orphan_ts = day + timedelta(hours=random.randint(9, 20))
            stls.append([f"S{stl_seq}", f"T{random.randint(900000, 999999)}", orphan_ts.isoformat(sep=" "), f"{random.uniform(500, 20000):.2f}", "SETTLED", f"B{orphan_ts.strftime('%Y%m%d')}-9"])

    dup = [list(e) for e in random.sample(evts, 12)]
    for e in dup:
        e[4] = e[4] + timedelta(minutes=random.randint(1, 10))
    evts.extend(dup)
    random.shuffle(evts)
    evts = [[e[0], e[1], e[2], e[3].isoformat(sep=" "), e[4].isoformat(sep=" "), e[5]] for e in evts]
    return txns, stls, evts, txn_seq, evt_seq, stl_seq


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"{path.name}: {len(rows)} rows")


def main():
    random.seed(SEED)
    RAW.mkdir(parents=True, exist_ok=True)
    mrows = merchant_rows()
    merchants = sorted({r[0] for r in mrows})
    write(RAW / "merchant.csv", ["merchant_id", "merchant_name", "merchant_category", "country", "risk_level", "effective_from", "effective_to"], mrows)

    problem = set(random.sample(merchants, 6))
    print("problem merchants:", sorted(problem))
    t, s, e, tseq, eseq, sseq = build(7, datetime(2026, 9, 1), 100000, 500000, 900000, merchants, problem)
    write(RAW / "transactions.csv", ["transaction_id", "merchant_id", "customer_id", "transaction_ts", "amount", "currency", "status", "payment_channel"], t)
    write(RAW / "settlements.csv", ["settlement_id", "transaction_id", "settlement_ts", "settlement_amount", "settlement_status", "settlement_batch"], s)
    write(RAW / "payment_events.csv", ["event_id", "transaction_id", "event_type", "event_ts", "ingestion_ts", "processing_ms"], e)

    t2, s2, e2, *_ = build(2, datetime(2026, 9, 8), tseq, eseq, sseq, merchants, problem)
    write(RAW / "transactions_20260909.csv", ["transaction_id", "merchant_id", "customer_id", "transaction_ts", "amount", "currency", "status", "payment_channel"], t2)
    write(RAW / "settlements_20260909.csv", ["settlement_id", "transaction_id", "settlement_ts", "settlement_amount", "settlement_status", "settlement_batch"], s2)
    write(RAW / "payment_events_20260909.csv", ["event_id", "transaction_id", "event_type", "event_ts", "ingestion_ts", "processing_ms"], e2)


if __name__ == "__main__":
    main()
