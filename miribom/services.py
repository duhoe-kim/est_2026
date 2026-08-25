from pathlib import Path
from collections import defaultdict
import csv

import joblib
import numpy as np
import pandas as pd

from django.conf import settings

MODEL_PATH    = Path(settings.BASE_DIR) / "model" / "lightgbm_model.pkl"
STATIONS_PATH = Path(settings.BASE_DIR) / "data" / "station_names.csv"
CHARGERS_PATH = Path(settings.BASE_DIR) / "data" / "chargers.csv"
HIST_HOUR     = Path(settings.BASE_DIR) / "data" / "hist_hour_map.csv"
HIST_WH       = Path(settings.BASE_DIR) / "data" / "hist_wh_map.csv"
CATEGORY_MAPS = Path(settings.BASE_DIR) / "data" / "category_maps.csv"

RADIUS_OPTIONS = {1: 0.08, 3: 0.03, 5: 0.015}
NEXT_RADIUS    = {1: 3, 3: 5, 5: None}
MIN_CANDIDATES = 3
LEVELS = [(0.25, "여유"), (0.50, "보통"), (999, "혼잡")]

model = joblib.load(MODEL_PATH)

station_names = pd.read_csv(STATIONS_PATH)
chargers_base = pd.read_csv(CHARGERS_PATH)

# ── 학습 때 쓴 피처 순서를 모델에서 직접 가져온다 ──────────────────────────
# (하드코딩하지 말 것: 순서/개수가 어긋나면 LightGBM이 컬럼을 못 찾는다)
FEATURES = list(model.feature_name_)          # 16개
CATEGORICAL_COLS = ["charger_pk", "유형(대분류)", "유형(소분류)", "시군구", "타입"]

# ── 과거 평균 사용빈도 테이블 → 조회용 Series(MultiIndex) ─────────────────
_hh = pd.read_csv(HIST_HOUR)
hist_hour_map = _hh.set_index(["charger_pk", "hour"])["사용빈도수"]

_hw = pd.read_csv(HIST_WH)
hist_wh_map = _hw.set_index(["charger_pk", "weekday", "hour"])["사용빈도수"]

GLOBAL_MEAN = float(_hh["사용빈도수"].mean())

category_maps = defaultdict(list)
with open(CATEGORY_MAPS, "r", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        category_maps[row["key"]].append(row["value"])
category_maps = dict(category_maps)


# ══════════════════════════════════════════════════════════════════════
#  내장 장소 사전 — station_names.csv 를 검색용으로 재활용
#  외부 검색 API 가 막혀도 최소한 제주 안 충전소 이름으로는 찾을 수 있게 합니다.
# ══════════════════════════════════════════════════════════════════════

def _norm(s):
    return "".join(str(s).lower().split())


_PLACE_INDEX = station_names.copy()
_PLACE_INDEX["_norm"] = _PLACE_INDEX["station_name"].map(_norm)


def search_station_names(query, limit=5):
    """
    내장 충전소 이름으로 장소를 찾는다. 토큰이 하나라도 들어가면 후보로 잡고,
    (일치한 토큰 수 → 이름이 짧은 순 → 충전기 많은 순) 으로 정렬한다.
    예) "제주 캠퍼스" → "한국폴리텍대학 제주캠퍼스"
    """
    q = _norm(query)
    if not q:
        return []

    tokens = [_norm(t) for t in str(query).split() if _norm(t)]
    if not tokens:
        tokens = [q]

    rows = []
    for _, r in _PLACE_INDEX.iterrows():
        name_n = r["_norm"]
        hits = sum(1 for t in tokens if t in name_n)
        if q in name_n:                 # 공백 뺀 전체 문자열이 그대로 들어가면 가장 강한 신호
            hits += len(tokens) + 1
        if not hits:
            continue
        rows.append((hits, len(name_n), int(r.get("charger_count") or 0), r))

    # 일치 토큰 많은 순 → 이름 짧은 순(군더더기 적은 매칭) → 충전기 많은 순
    rows.sort(key=lambda x: (-x[0], x[1], -x[2]))

    out = []
    for _, _, _, r in rows[:limit]:
        out.append({
            "name": str(r["station_name"]),
            "address": "제주 · 급속충전소",
            "category": "",
            "lat": float(r["위도"]), "lng": float(r["경도"]),
            "source": "충전소 데이터",
        })
    return out


def level_of(v):
    for thr, name in LEVELS:
        if v < thr:
            return name
    return "혼잡"


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * R * np.arcsin(np.sqrt(a))


def attach_station_name(stations):
    stations = stations.copy()
    stations["_k"] = (stations["위도"].round(5).astype(str) + "_"
                      + stations["경도"].round(5).astype(str))
    nm = station_names.copy()
    nm["_k"] = (nm["위도"].round(5).astype(str) + "_"
                + nm["경도"].round(5).astype(str))
    stations = stations.merge(nm[["_k", "station_name"]], on="_k", how="left")
    fallback = stations["시군구"].astype(str) + " 충전소"
    stations["station_name"] = stations["station_name"].fillna(fallback)
    return stations.drop(columns="_k")


def _lookup_hour(pk, hour):
    """(charger_pk, hour) 평균 사용빈도. 없으면 전체 평균."""
    idx = pd.MultiIndex.from_arrays([pk, hour])
    return pd.Series(hist_hour_map.reindex(idx).to_numpy(), index=pk.index).fillna(GLOBAL_MEAN)


def _lookup_wh(pk, weekday, hour):
    """(charger_pk, weekday, hour) 평균 사용빈도. 없으면 시간대 평균."""
    idx = pd.MultiIndex.from_arrays([pk, weekday, hour])
    return pd.Series(hist_wh_map.reindex(idx).to_numpy(), index=pk.index)


def build_features(target_time):
    """
    미래 시점 예측이라 실제 usage_lag_* 값이 존재하지 않는다.
    → 과거 평균 사용빈도 테이블로 대체한다.
        usage_lag_1h   ≈ (충전기, 1시간 전 시각) 평균
        usage_lag_2h   ≈ (충전기, 2시간 전 시각) 평균
        usage_lag_24h  ≈ (충전기, 같은 시각) 평균            = 하루 전 같은 시각
        usage_lag_168h ≈ (충전기, 같은 요일·시각) 평균        = 일주일 전 같은 시각
    """
    df = chargers_base.copy()

    hour    = target_time.hour
    weekday = target_time.dayofweek

    df["hour"]       = hour
    df["weekday"]    = weekday
    df["month"]      = target_time.month
    df["is_weekend"] = int(weekday >= 5)

    pk = df["charger_pk"]

    h1 = pd.Series((hour - 1) % 24, index=df.index)
    h2 = pd.Series((hour - 2) % 24, index=df.index)
    hh = pd.Series(hour, index=df.index)
    wd = pd.Series(weekday, index=df.index)

    df["usage_lag_1h"]   = _lookup_hour(pk, h1)
    df["usage_lag_2h"]   = _lookup_hour(pk, h2)
    df["usage_lag_24h"]  = _lookup_hour(pk, hh)
    df["usage_lag_168h"] = _lookup_wh(pk, wd, hh).fillna(df["usage_lag_24h"])

    # 참고용(모델 입력 아님)
    df["hist_charger_hour"]         = df["usage_lag_24h"]
    df["hist_charger_weekday_hour"] = df["usage_lag_168h"]

    # 카테고리는 학습 때와 '같은 categories 순서'로 맞춰야 코드가 일치한다
    for col in CATEGORICAL_COLS:
        df[col] = pd.Categorical(df[col].astype("string"),
                                 categories=category_maps[col])

    missing = [c for c in FEATURES if c not in df.columns]
    if missing:
        raise KeyError(f"모델이 요구하는 피처가 없습니다: {missing}")

    return df


def filter_by_kw(df, kw):
    """출력 용량 조건. 0(또는 None)이면 전체."""
    kw = int(kw or 0)
    if kw == 50:
        return df[(df["용량_kw"] >= 50) & (df["용량_kw"] < 100)]
    if kw == 100:
        return df[(df["용량_kw"] >= 100) & (df["용량_kw"] < 200)]
    if kw == 200:
        return df[df["용량_kw"] >= 200]
    return df


def build_reasons(row, ctx):
    """
    이 충전소를 추천한 이유를 사람이 읽을 문장으로 만든다.
    순위 자체는 (혼잡 확률 + 거리 가중치) 점수로 정해지므로,
    그 점수를 끌어올린 요인을 중요한 순서대로 골라 준다.
    """
    reasons = []
    p    = float(row["congestion_prob"])
    d    = float(row["distance_km"])
    n    = int(row["charger_count"])
    kw   = int(row["max_power_kw"])
    pct  = round(p * 100)

    # 1) 혼잡도 — 추천의 1순위 근거
    if p <= ctx["min_prob"] + 1e-9:
        reasons.append(f"반경 안에서 예상 혼잡도가 가장 낮아요 (혼잡 확률 {pct}%)")
    elif row["혼잡도"] == "여유":
        reasons.append(f"이 시간대 혼잡 확률이 {pct}%로 여유로울 전망이에요")
    elif row["혼잡도"] == "보통":
        reasons.append(f"혼잡 확률 {pct}%로 무난한 편이에요")
    else:
        reasons.append(f"혼잡 확률 {pct}%로 붐빌 수 있지만 조건 안에서는 나은 편이에요")

    # 2) 거리
    if d <= ctx["min_dist"] + 1e-9:
        reasons.append(f"반경 안에서 가장 가까워요 ({d:.1f}km)")
    elif d <= 1.0:
        reasons.append(f"{d:.1f}km로 바로 근처예요")
    elif d <= ctx["radius"] / 2:
        reasons.append(f"{d:.1f}km로 반경 안에서도 가까운 편이에요")

    # 3) 충전기 대수 — 대기 위험
    if n >= 3:
        reasons.append(f"충전기가 {n}대라 한 대가 차 있어도 대기 위험이 낮아요")
    elif n == 1:
        reasons.append("충전기가 1대뿐이라 도착 전 확인을 권해요")

    # 4) 출력
    if kw >= 200:
        reasons.append(f"{kw}kW 초급속이라 충전이 빨라요")
    elif kw >= 100:
        reasons.append(f"{kw}kW 고출력이라 충전이 빨라요")

    return reasons[:3]


def make_prediction(date, time, dist, kw, lon, lat, top_n=3):
    # ── 입력값 형변환 (GET 파라미터는 전부 문자열로 들어온다) ──
    time = int(str(time).split(":")[0])
    dist = int(dist)
    lon  = float(lon)
    lat  = float(lat)

    if not 0 <= time <= 23:
        raise ValueError("시간은 0~23 사이여야 합니다.")
    if dist not in RADIUS_OPTIONS:
        raise ValueError(f"반경은 {list(RADIUS_OPTIONS)} 중 하나여야 합니다.")

    distance_weight = RADIUS_OPTIONS[dist]
    target_time = pd.Timestamp(date) + pd.Timedelta(hours=time)

    chargers = build_features(target_time)
    chargers["congestion_prob"] = model.predict(chargers[FEATURES]).clip(0, 1)

    chargers = filter_by_kw(chargers, kw)
    if chargers.empty:
        return {"ok": False,
                "message": "선택하신 출력 용량 조건에 맞는 급속충전소가 없습니다."}

    stations = (chargers.groupby(["경도", "위도"], as_index=False, observed=True)
                .agg(시군구=("시군구", "first"),
                     충전소유형=("유형(대분류)", "first"),
                     charger_count=("charger_pk", "nunique"),
                     max_power_kw=("용량_kw", "max"),
                     congestion_prob=("congestion_prob", "mean")))

    stations["혼잡도"] = stations["congestion_prob"].map(level_of)
    stations = attach_station_name(stations)

    stations["distance_km"] = haversine(lat, lon,
                                        stations["위도"].to_numpy(),
                                        stations["경도"].to_numpy())

    min_dist = float(stations["distance_km"].min())
    all_stations = stations
    stations = stations[stations["distance_km"] <= dist]

    if stations.empty:
        nxt = NEXT_RADIUS[dist]
        msg = (f"{dist}km 안에 급속충전소가 없습니다. "
               f"가장 가까운 곳은 {min_dist:.1f}km 떨어져 있습니다.")
        if nxt and min_dist <= nxt:
            n_next = int((all_stations["distance_km"] <= nxt).sum())
            msg += f" {nxt}km까지 넓히면 {n_next}곳을 비교할 수 있습니다."
        return {"ok": False, "message": msg}

    stations = stations.copy()
    stations["score"] = (stations["congestion_prob"]
                         + distance_weight * stations["distance_km"])

    top = stations.nsmallest(min(top_n, len(stations)), "score").reset_index(drop=True)
    top.insert(0, "순위", range(1, len(top) + 1))
    top["distance_km"]     = top["distance_km"].round(2)
    top["congestion_prob"] = top["congestion_prob"].round(4)

    table = top[["순위", "station_name", "혼잡도", "congestion_prob", "distance_km",
                 "charger_count", "max_power_kw", "충전소유형", "시군구", "위도", "경도"]]

    # 추천 이유를 만들 때 쓰는 '반경 안 전체' 기준값
    ctx = {
        "min_prob": float(stations["congestion_prob"].min()),
        "min_dist": float(stations["distance_km"].min()),
        "radius": float(dist),
    }

    nearest = stations.nsmallest(1, "distance_km").iloc[0]
    payload = {
        "ok": True,
        "when": target_time.isoformat(),
        "origin": {"lat": lat, "lng": lon},
        "radius_km": dist,
        "n_candidates": len(stations),
        "nearest": {"name": nearest["station_name"],
                    "distance_km": round(float(nearest["distance_km"]), 2),
                    "congestion_prob": round(float(nearest["congestion_prob"]), 4),
                    "level": nearest["혼잡도"],
                    "chargers": int(nearest["charger_count"])},
        "items": [{"rank": int(r["순위"]), "name": r["station_name"],
                   "lat": float(r["위도"]), "lng": float(r["경도"]),
                   "distance_km": float(r["distance_km"]),
                   "congestion_prob": float(r["congestion_prob"]),
                   "level": r["혼잡도"], "chargers": int(r["charger_count"]),
                   "max_power_kw": int(r["max_power_kw"]),
                   "station_type": str(r["충전소유형"]), "sigungu": str(r["시군구"]),
                   "reasons": build_reasons(r, ctx)}
                  for _, r in table.iterrows()],
    }
    return payload
