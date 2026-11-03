from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from engine.core.models import Index
from engine.data.global_markets import GlobalMarkets, overnight_score, parse_chart
from engine.data.nse import NseClient, parse_fii_dii, parse_holidays, parse_participant_oi, positioning_score, unzip_first_csv
from engine.data.object_storage import ObjectStorage
from tests.conftest import DAY, at

PARTICIPANT_CSV = """"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Nov 02, 2026"
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short\t,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts\t,Total Short Contracts
Client,"250000","150000",1,1,900000,800000,700000,650000,1,1,1,1,1,1
DII,10000,60000,1,1,0,10,0,0,1,1,1,1,1,1
FII,60000,180000,1,1,200000,250000,220000,210000,1,1,1,1,1,1
Pro,80000,10000,1,1,300000,350000,480000,540000,1,1,1,1,1,1
TOTAL,400000,400000,4,4,1400000,1400010,1400000,1400000,4,4,4,4,4,4
"""

HOLIDAYS = {"CM": [], "FO": [{"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day", "Sr_no": 1},
                             {"tradingDate": "10-Nov-2026", "weekDay": "Tuesday", "description": "Diwali", "Sr_no": 2}, {"bad": 1}]}
FIIDII = [{"category": "DII **", "date": "02-Nov-2026", "buyValue": "15000.5", "sellValue": "12000", "netValue": "3000.5"},
          {"category": "FII/FPI **", "date": "02-Nov-2026", "buyValue": "11000", "sellValue": "14500.25", "netValue": "-3500.25"}]


def test_parse_holidays_and_flows():
    assert parse_holidays(HOLIDAYS) == [date(2026, 1, 26), date(2026, 11, 10)]
    rows = parse_fii_dii(FIIDII)
    assert [(r.category, r.net_cr) for r in rows] == [("DII", 3000.5), ("FII/FPI", -3500.25)]


def test_participant_oi_and_positioning():
    p = parse_participant_oi(PARTICIPANT_CSV)
    assert set(p) == {"Client", "DII", "FII", "Pro", "TOTAL"}
    assert (p["FII"].fut_index_long, p["FII"].fut_index_short, p["Client"].call_long) == (60000, 180000, 900000)
    s = positioning_score(p)
    assert s["fii_index_fut_net_ratio"] == pytest.approx(-0.5) and s["bias"] == pytest.approx(-0.5) and s["squeeze_risk"] == 0.0
    prev = parse_participant_oi(PARTICIPANT_CSV.replace("FII,60000,180000", "FII,100000,140000"))
    s2 = positioning_score(p, prev)
    assert s2["fii_change"] == pytest.approx(-0.5 - (-1 / 6), abs=1e-3) and s2["bias"] < s["bias"]


def test_unzip_bhavcopy():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy.csv", "a,b\n1,2\n")
    assert unzip_first_csv(buf.getvalue()).startswith("a,b")


async def test_nse_client_cookie_bootstrap_rate_limit_and_cache(cfg, tmp_path, monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/":
            return httpx.Response(200, text="home", headers={"set-cookie": "nsit=abc; Path=/"})
        if "holiday" in str(req.url):
            assert req.headers.get("cookie", "").startswith("nsit=")
            return httpx.Response(200, json=HOLIDAYS)
        if "participant" in str(req.url):
            return httpx.Response(200, text=PARTICIPANT_CSV)
        return httpx.Response(404)

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("engine.data.nse.asyncio.sleep", fake_sleep)
    nse = NseClient(cfg.feeds.nse, tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://www.nseindia.com"))
    assert (await nse.holidays(DAY))[-1] == date(2026, 11, 10)
    oi = await nse.participant_oi(date(2026, 11, 2))
    assert "02112026" in seen[-1] and oi["FII"].fut_index_short == 180000
    assert sleeps and sleeps[0] > 2      # second request waited ~3s
    n = len(seen)
    await nse.holidays(DAY)
    assert len(seen) == n                # cached on disk for the day
    with pytest.raises(httpx.HTTPStatusError):
        await nse.fii_dii(DAY)


def chart(last, prev, start_ts=1_762_100_000, closes=(100.0, 101.0, 102.0)):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": last, "chartPreviousClose": prev}, "timestamp": [start_ts + i * 300 for i in range(len(closes))],
                                  "indicators": {"quote": [{"close": list(closes) + [None]}]}}]}}


def test_parse_chart_and_moves():
    s = parse_chart(chart(102.0, 100.0), "X")
    assert s is not None and s.change_pct == pytest.approx(2.0) and len(s.points) == 3
    t1 = s.points[1][0]
    assert s.move_since(t1) == pytest.approx((102 / 101 - 1) * 100)
    assert parse_chart({"chart": {"result": None}}, "X") is None


def test_overnight_score_signs(cfg):
    g = cfg.feeds.global_markets
    up = {"US_FUTURES": parse_chart(chart(101, 100), "US_FUTURES"), "CRUDE": parse_chart(chart(95, 100), "CRUDE")}
    s = overnight_score(up, g)
    assert s["score"] > 0 and s["weights"].startswith("priors") and "weight 0" in s["gift_nifty"]
    down = {"DXY": parse_chart(chart(102, 100), "DXY")}
    assert overnight_score(down, g)["score"] < 0
    assert overnight_score({}, g)["score"] is None


async def test_global_refresh_tolerates_failures(cfg):
    def handler(req: httpx.Request) -> httpx.Response:
        if "CL%3DF" in str(req.url) or "CL=F" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json=chart(101, 100))

    gm = GlobalMarkets(cfg.feeds.global_markets, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    series = await gm.refresh()
    assert "CRUDE" not in series and "US_FUTURES" in series and gm.score()["score"] is not None
    assert gm.moves_since(datetime(2030, 1, 1, tzinfo=timezone.utc))["US_FUTURES"] is not None


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_file(self, path, bucket, key):
        self.objects[(bucket, key)] = Path(path).read_bytes()

    def download_file(self, bucket, key, target):
        Path(target).write_bytes(self.objects[(bucket, key)])

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)


def test_object_storage_keys_and_idempotent_upload(tmp_path):
    f = tmp_path / "a.parquet"
    f.write_bytes(b"v1")
    st = ObjectStorage("e", "bkt", "k", "s", prefix="/papertrade/", client=FakeS3())
    key = st.upload(f, "ticks", "date=2026-11-03", "ticks.parquet")
    assert key == "papertrade/ticks/date=2026-11-03/ticks.parquet" and st.exists("ticks", "date=2026-11-03", "ticks.parquet")
    f.write_bytes(b"v2")
    st.upload(f, "ticks", "date=2026-11-03", "ticks.parquet")
    assert st.download(tmp_path / "out.bin", "ticks", "date=2026-11-03", "ticks.parquet").read_bytes() == b"v2"


@pytest.mark.db
def test_backup_export(cfg, tmp_path):
    from engine.config.settings import Settings
    from engine.db.session import make_engine
    from engine.store.backup import export_tables
    from engine.store.sql import SqlStore
    from tests.trade_factory import WIN, make_trade

    eng = make_engine(Settings().database_url)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    SqlStore(eng).save_trade(make_trade(cfg, DAY, 1, WIN))
    paths = export_tables(eng, tmp_path)
    import pyarrow.parquet as pq

    names = {p.stem for p in paths}
    assert {"trades", "trade_legs", "signals", "reports"} <= names
    assert pq.read_table(tmp_path / "trades.parquet").num_rows == 1
