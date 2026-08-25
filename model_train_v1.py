"""
제주 급속충전소 혼잡 예측 및 추천

Colab /content/ 에 업로드할 파일:
  - train_jeju.parquet      (2024-01 ~ 2025-12)
  - test_jeju.parquet       (2026-01 ~ 2026-06)
  - station_names.csv       (충전소 좌표 → 이름)
"""

# !pip install lightgbm ipywidgets -q

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import mean_absolute_error, mean_squared_error


# ============================================================
# 1. 데이터 로드
#    train/test는 합치지 않는다. train으로 학습하고 test로 검증한 뒤,
#    검증한 그 모델을 그대로 서비스에 사용한다.
# ============================================================
train = pd.read_parquet("/content/train_jeju.parquet")
test = pd.read_parquet("/content/test_jeju.parquet")

train["사용시간"] = pd.to_datetime(train["사용시간"])
test["사용시간"] = pd.to_datetime(test["사용시간"])

train = train.sort_values(["charger_pk", "사용시간"]).reset_index(drop=True)
test = test.sort_values(["charger_pk", "사용시간"]).reset_index(drop=True)

print(f"Train {len(train):,}행 | {train['사용시간'].min():%Y-%m-%d} ~ {train['사용시간'].max():%Y-%m-%d}")
print(f"Test  {len(test):,}행 | {test['사용시간'].min():%Y-%m-%d} ~ {test['사용시간'].max():%Y-%m-%d}")


# ============================================================
# 2. 시간 파생변수
# ============================================================
for df in (train, test):
    df["hour"] = df["사용시간"].dt.hour.astype("int8")
    df["weekday"] = df["사용시간"].dt.dayofweek.astype("int8")
    df["month"] = df["사용시간"].dt.month.astype("int8")
    df["is_weekend"] = (df["weekday"] >= 5).astype("int8")


# ============================================================
# 3. 범주형 컬럼
#    category 체계는 train 기준으로 고정한다. test와 서빙에서 동일하게 복원해야
#    모델이 학습한 것과 같은 코드 값을 받는다.
# ============================================================
categorical_cols = ["charger_pk", "유형(대분류)", "유형(소분류)", "시군구", "타입"]

for col in categorical_cols:
    train[col] = train[col].astype("string").fillna("UNKNOWN")
    test[col] = test[col].astype("string").fillna("UNKNOWN")

category_maps = {col: list(train[col].unique()) for col in categorical_cols}


# ============================================================
# 4. 과거 패턴 Feature
#    train: shift(1)로 현재 행의 target이 자기 피처에 들어가지 않게 막는다
#    test : train에서만 만든 평균표를 적용해 미래 정보가 새지 않게 한다
# ============================================================
target = "사용빈도수"

train["hist_charger_hour"] = (
    train.groupby(["charger_pk", "hour"], observed=True)[target]
    .transform(lambda x: x.shift(1).expanding().mean())
    .astype("float32")
)
train["hist_charger_weekday_hour"] = (
    train.groupby(["charger_pk", "weekday", "hour"], observed=True)[target]
    .transform(lambda x: x.shift(1).expanding().mean())
    .astype("float32")
)

hist_hour_map = train.groupby(["charger_pk", "hour"], observed=True)[target].mean()
hist_wh_map = train.groupby(["charger_pk", "weekday", "hour"], observed=True)[target].mean()

test["hist_charger_hour"] = (
    pd.MultiIndex.from_frame(test[["charger_pk", "hour"]])
    .map(hist_hour_map).astype("float32")
)
test["hist_charger_weekday_hour"] = (
    pd.MultiIndex.from_frame(test[["charger_pk", "weekday", "hour"]])
    .map(hist_wh_map).astype("float32")
)


# ============================================================
# 5. Feature 목록 및 category 적용
# ============================================================
features = [
    "charger_pk",
    "hour", "weekday", "month", "is_weekend",
    "유형(대분류)", "유형(소분류)", "시군구", "타입", "용량_kw",
    "경도", "위도",
    "usage_lag_1h", "usage_lag_2h", "usage_lag_24h", "usage_lag_168h",
    "hist_charger_hour", "hist_charger_weekday_hour",
]

for col in categorical_cols:
    train[col] = pd.Categorical(train[col], categories=category_maps[col])
    test[col] = pd.Categorical(test[col], categories=category_maps[col])

X_train, y_train = train[features], train[target]
X_test, y_test = test[features], test[target]


# ============================================================
# 6. 모델 학습
# ============================================================
model = lgb.LGBMRegressor(
    objective="regression",
    n_estimators=500,
    learning_rate=0.05,
    num_leaves=31,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    n_jobs=-1,
)
model.fit(X_train, y_train, categorical_feature=categorical_cols)
print("\n학습 완료 | 충전기", train["charger_pk"].nunique(), "대")


# ============================================================
# 7. 성능 평가 (Historical Average 대비)
# ============================================================
lgb_pred = np.clip(model.predict(X_test), 0, None)

# 기준선: 같은 충전기 + 같은 요일 + 같은 시각의 train 평균
hist_pred = test["hist_charger_weekday_hour"].fillna(y_train.mean()).to_numpy()


def report(y, p, name):
    mae = mean_absolute_error(y, p)
    rmse = np.sqrt(mean_squared_error(y, p))
    print(f"  {name:24s} MAE {mae:.4f} | RMSE {rmse:.4f}")
    return mae, rmse


print("\n전체 구간")
h_mae, h_rmse = report(y_test, hist_pred, "Historical Average")
m_mae, m_rmse = report(y_test, lgb_pred, "LightGBM")
print(f"  개선율: MAE {(h_mae-m_mae)/h_mae*100:+.2f}% | RMSE {(h_rmse-m_rmse)/h_rmse*100:+.2f}%")

# 0이 82%라 전체 지표는 '0을 맞힌 성과'에 가려진다. 실제 이용 구간을 따로 본다.
act = y_test.to_numpy() > 0
print(f"\n실제 이용 발생 구간 ({act.sum():,}행, 전체의 {act.mean()*100:.1f}%)")
ha_mae, _ = report(y_test.to_numpy()[act], hist_pred[act], "Historical Average")
la_mae, _ = report(y_test.to_numpy()[act], lgb_pred[act], "LightGBM")
print(f"  개선율: MAE {(ha_mae-la_mae)/ha_mae*100:+.2f}%")

imp = (pd.DataFrame({"feature": features, "importance": model.feature_importances_})
       .sort_values("importance", ascending=False).reset_index(drop=True))
print("\n상위 피처:", ", ".join(imp.head(6)["feature"]))


# ============================================================
# 8. 추천 로직
# ============================================================
# 반경 선택지와 그에 맞는 거리 가중치.
# 1km를 고른 사용자는 이미 '가까운 곳 우선'을 선언한 셈이므로 거리에 민감하게,
# 5km를 고른 사용자는 이동을 감수할 의향을 밝힌 것이므로 혼잡도를 더 크게 본다.
#
# 제주 충전소 밀도(211개소): 1km 중앙값 1개 대안 / 3km 4개 / 5km 9개
RADIUS_OPTIONS = {1: 0.08, 3: 0.03, 5: 0.015}
NEXT_RADIUS = {1: 3, 3: 5, 5: None}
MIN_CANDIDATES = 3
LEVELS = [(0.10, "여유"), (0.25, "보통"), (999, "혼잡")]

station_names = pd.read_csv("/content/station_names.csv")

# 서빙 기준 스냅샷: 가장 최근 관측(test 마지막)에서 충전기별 1행씩
snapshot = (pd.concat([train, test], ignore_index=True)
            .sort_values("사용시간")
            .groupby("charger_pk", observed=True).tail(1)
            .reset_index(drop=True))


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
    """좌표로 충전소명을 붙인다. 부동소수점 오차를 감안해 반올림 키로 조인."""
    stations = stations.copy()
    stations["_k"] = (stations["위도"].round(5).astype(str) + "_"
                      + stations["경도"].round(5).astype(str))
    nm = station_names.copy()
    nm["_k"] = (nm["위도"].round(5).astype(str) + "_"
                + nm["경도"].round(5).astype(str))
    stations = stations.merge(nm[["_k", "station_name"]], on="_k", how="left")
    # 시군구가 Categorical이면 문자열과 바로 이을 수 없으므로 astype(str)로 푼다
    fallback = stations["시군구"].astype(str) + " 충전소"
    stations["station_name"] = stations["station_name"].fillna(fallback)
    return stations.drop(columns="_k")


def recommend(date, hour, lat, lon, radius_km=3, top_n=3):
    """미래 시점 충전소 추천.

    date      : "2026-08-21"
    hour      : 0~23
    radius_km : 1 / 3 / 5
    반환      : (표 DataFrame, 지도/API용 dict)
    """
    if not 0 <= hour <= 23:
        raise ValueError("시간은 0~23 사이여야 합니다.")
    if radius_km not in RADIUS_OPTIONS:
        raise ValueError(f"반경은 {list(RADIUS_OPTIONS)} 중 하나여야 합니다.")

    distance_weight = RADIUS_OPTIONS[radius_km]
    target_time = pd.Timestamp(date) + pd.Timedelta(hours=hour)

    chargers = snapshot.copy()
    for col in categorical_cols:
        chargers[col] = chargers[col].astype("string")

    # 예측 시점의 시간 파생변수로 덮어쓴다
    chargers["hour"] = target_time.hour
    chargers["weekday"] = target_time.dayofweek
    chargers["month"] = target_time.month
    chargers["is_weekend"] = int(target_time.dayofweek >= 5)

    # 과거 패턴은 예측 시점의 요일/시각 기준으로 다시 조회
    chargers["hist_charger_hour"] = (
        pd.MultiIndex.from_frame(chargers[["charger_pk", "hour"]]).map(hist_hour_map))
    chargers["hist_charger_weekday_hour"] = (
        pd.MultiIndex.from_frame(chargers[["charger_pk", "weekday", "hour"]]).map(hist_wh_map))

    for col in categorical_cols:
        chargers[col] = pd.Categorical(chargers[col], categories=category_maps[col])

    chargers["predicted_usage"] = np.clip(model.predict(chargers[features]), 0, None)

    # ── 충전기 → 충전소 집계 ──
    # 평균을 쓰면 6대 충전소와 1대 충전소가 같은 척도가 되어 다충전기 충전소를
    # 불리하게 평가한다. 전체 예상 수요를 대수로 나눠 '충전기 1대당 부하'로 비교한다.
    stations = (chargers.groupby(["경도", "위도"], as_index=False, observed=True)
                .agg(시군구=("시군구", "first"),
                     충전소유형=("유형(대분류)", "first"),
                     charger_count=("charger_pk", "nunique"),
                     max_power_kw=("용량_kw", "max"),
                     total_usage=("predicted_usage", "sum")))

    stations["load_per_charger"] = stations["total_usage"] / stations["charger_count"]
    stations["혼잡도"] = stations["load_per_charger"].map(level_of)
    stations = attach_station_name(stations)

    stations["distance_km"] = haversine(lat, lon,
                                        stations["위도"].to_numpy(),
                                        stations["경도"].to_numpy())

    min_dist = float(stations["distance_km"].min())
    all_stations = stations
    stations = stations[stations["distance_km"] <= radius_km]
    if stations.empty:
        nxt = NEXT_RADIUS[radius_km]
        msg = (f"{radius_km}km 안에 급속충전소가 없습니다. "
               f"가장 가까운 곳은 {min_dist:.1f}km 떨어져 있습니다.")
        if nxt and min_dist <= nxt:
            n_next = int((all_stations["distance_km"] <= nxt).sum())
            msg += f" {nxt}km까지 넓히면 {n_next}곳을 비교할 수 있습니다."
        return pd.DataFrame(), {"ok": False, "message": msg}

    # ── 순위 산정 ──
    # 정렬 키를 나열하면 혼잡도가 0.0001만 낮아도 무조건 앞서고 거리는 동점일 때만 쓰인다.
    # 결과적으로 '멀리 있는 한산한 곳'이 1위가 되므로 가중 점수로 합쳐 비교한다.
    stations = stations.copy()
    stations["score"] = (stations["load_per_charger"]
                         + distance_weight * stations["distance_km"])

    top = stations.nsmallest(min(top_n, len(stations)), "score").reset_index(drop=True)
    top.insert(0, "순위", range(1, len(top) + 1))
    top["distance_km"] = top["distance_km"].round(2)
    top["load_per_charger"] = top["load_per_charger"].round(4)

    table = top[["순위", "station_name", "혼잡도", "load_per_charger", "distance_km",
                 "charger_count", "max_power_kw", "충전소유형", "시군구", "위도", "경도"]]

    nearest = stations.nsmallest(1, "distance_km").iloc[0]
    payload = {
        "ok": True,
        "when": target_time.isoformat(),
        "origin": {"lat": lat, "lng": lon},
        "radius_km": radius_km,
        "n_candidates": len(stations),
        "nearest": {"name": nearest["station_name"],
                    "distance_km": round(float(nearest["distance_km"]), 2),
                    "load_per_charger": round(float(nearest["load_per_charger"]), 4),
                    "level": nearest["혼잡도"],
                    "chargers": int(nearest["charger_count"])},
        "items": [{"rank": int(r["순위"]), "name": r["station_name"],
                   "lat": float(r["위도"]), "lng": float(r["경도"]),
                   "distance_km": float(r["distance_km"]),
                   "load_per_charger": float(r["load_per_charger"]),
                   "level": r["혼잡도"], "chargers": int(r["charger_count"]),
                   "max_power_kw": int(r["max_power_kw"])}
                  for _, r in table.iterrows()],
    }

    nxt = NEXT_RADIUS[radius_km]
    if len(stations) < MIN_CANDIDATES and nxt:
        n_next = int((all_stations["distance_km"] <= nxt).sum())
        if n_next > len(stations):
            payload["suggestion"] = (f"{radius_km}km 안에는 {len(stations)}곳뿐입니다. "
                                     f"{nxt}km까지 넓히면 {n_next}곳을 비교할 수 있습니다.")
    return table, payload


def show(date, hour, lat, lon, radius_km):
    table, payload = recommend(date, hour, lat, lon, radius_km)
    if not payload["ok"]:
        print(payload["message"])
        return
    display(table)
    n, b = payload["nearest"], payload["items"][0]
    print(f"반경 {radius_km}km 내 후보 {payload['n_candidates']}개소")
    print(f"가장 가까운 곳: {n['name']} {n['distance_km']}km "
          f"(충전기 {n['chargers']}대, {n['level']})")
    print(f"추천 1위     : {b['name']} {b['distance_km']}km "
          f"(충전기 {b['chargers']}대, {b['level']})")
    if "suggestion" in payload:
        print(payload["suggestion"])


# ============================================================
# 9. 입력 위젯
#    위젯을 못 쓰는 환경이면 아래 기본값으로 한 번 실행된다.
# ============================================================
DATE, HOUR, LAT, LON, RADIUS = "2026-08-21", 15, 33.4996, 126.5312, 3

try:
    import ipywidgets as w
    from IPython.display import display as _d

    w_date = w.DatePicker(description="날짜", value=pd.Timestamp(DATE).date())
    w_hour = w.IntSlider(description="시간", min=0, max=23, value=HOUR)
    w_lat = w.FloatText(description="위도", value=LAT, step=0.001)
    w_lon = w.FloatText(description="경도", value=LON, step=0.001)
    w_rad = w.ToggleButtons(description="반경", options=[("1km", 1), ("3km", 3), ("5km", 5)],
                            value=RADIUS)
    w_btn = w.Button(description="추천 받기", button_style="primary")
    w_out = w.Output()

    def _on_click(_):
        with w_out:
            w_out.clear_output()
            if w_date.value is None:
                print("날짜를 선택해주세요.")
                return
            show(str(w_date.value), w_hour.value, w_lat.value, w_lon.value, w_rad.value)

    w_btn.on_click(_on_click)
    _d(w.VBox([w.HBox([w_date, w_hour]), w.HBox([w_lat, w_lon]), w_rad, w_btn, w_out]))
    _on_click(None)

except Exception as e:
    print(f"위젯 사용 불가({type(e).__name__}) — 기본값으로 실행합니다.")
    show(DATE, HOUR, LAT, LON, RADIUS)
