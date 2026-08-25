"""
제주 급속충전소 혼잡 예측 및 추천  (분류 통일 버전)
- 모델 선정 단계에서 AUC 기준으로 LightGBM(binary)을 선정 → 서비스도 동일 모델 사용
- 타깃: 혼잡여부(0/1),  예측: 혼잡 확률(0~1)
"""
# !pip install lightgbm ipywidgets -q
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, log_loss

# ============================================================
# 1. 데이터 로드
# ============================================================
train = pd.read_parquet("/content/train_jeju.parquet")
test  = pd.read_parquet("/content/test_jeju.parquet")

train["사용시간"] = pd.to_datetime(train["사용시간"])
test["사용시간"]  = pd.to_datetime(test["사용시간"])

train = train.sort_values(["charger_pk", "사용시간"]).reset_index(drop=True)
test  = test.sort_values(["charger_pk", "사용시간"]).reset_index(drop=True)

print(f"Train {len(train):,}행 | {train['사용시간'].min():%Y-%m-%d} ~ {train['사용시간'].max():%Y-%m-%d}")
print(f"Test  {len(test):,}행 | {test['사용시간'].min():%Y-%m-%d} ~ {test['사용시간'].max():%Y-%m-%d}")

# ============================================================
# 2. 시간 파생변수
# ============================================================
for df in (train, test):
    df["hour"]       = df["사용시간"].dt.hour.astype("int8")
    df["weekday"]    = df["사용시간"].dt.dayofweek.astype("int8")
    df["month"]      = df["사용시간"].dt.month.astype("int8")
    df["is_weekend"] = (df["weekday"] >= 5).astype("int8")

# ============================================================
# 3. 타깃 정의 (혼잡여부)
#    사용빈도수 > 0 이면 '혼잡'.  분위수 기준으로 바꾸려면 THR만 수정.
# ============================================================
THR = 0
train["혼잡여부"] = (train["사용빈도수"] > THR).astype("int8")
test["혼잡여부"]  = (test["사용빈도수"]  > THR).astype("int8")

# ============================================================
# 4. 범주형 컬럼 (train 기준으로 category 고정)
# ============================================================
categorical_cols = ["charger_pk", "유형(대분류)", "유형(소분류)", "시군구", "타입"]

for col in categorical_cols:
    train[col] = train[col].astype("string").fillna("UNKNOWN")
    test[col]  = test[col].astype("string").fillna("UNKNOWN")

category_maps = {col: list(train[col].unique()) for col in categorical_cols}

# ============================================================
# 5. 과거 패턴 Feature (누수 방지: train은 shift(1), test는 train 평균표 map)
# ============================================================
rate = "혼잡여부"   # 과거 '혼잡 발생률'을 피처로 사용

train["hist_charger_hour"] = (
    train.groupby(["charger_pk", "hour"], observed=True)[rate]
         .transform(lambda x: x.shift(1).expanding().mean())
         .astype("float32"))
train["hist_charger_weekday_hour"] = (
    train.groupby(["charger_pk", "weekday", "hour"], observed=True)[rate]
         .transform(lambda x: x.shift(1).expanding().mean())
         .astype("float32"))

hist_hour_map = train.groupby(["charger_pk", "hour"], observed=True)[rate].mean()
hist_wh_map   = train.groupby(["charger_pk", "weekday", "hour"], observed=True)[rate].mean()

test["hist_charger_hour"] = (
    pd.MultiIndex.from_frame(test[["charger_pk", "hour"]]).map(hist_hour_map).astype("float32"))
test["hist_charger_weekday_hour"] = (
    pd.MultiIndex.from_frame(test[["charger_pk", "weekday", "hour"]]).map(hist_wh_map).astype("float32"))

# ============================================================
# 6. Feature 목록 및 category 적용
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
    test[col]  = pd.Categorical(test[col],  categories=category_maps[col])

X_train, y_train = train[features], train["혼잡여부"]
X_test,  y_test  = test[features],  test["혼잡여부"]

# ============================================================
# 7. 모델 학습 (binary — AUC로 선정한 그 LightGBM)
# ============================================================
train_set = lgb.Dataset(X_train, label=y_train, categorical_feature=categorical_cols)
valid_set = lgb.Dataset(X_test,  label=y_test,  categorical_feature=categorical_cols,
                        reference=train_set)

params = {
    "objective": "binary",
    "metric": ["auc", "binary_logloss"],
    "learning_rate": 0.05,
    "num_leaves": 31,
    "verbose": -1,
}
model = lgb.train(
    params, train_set, num_boost_round=500,
    valid_sets=[valid_set],
    callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
)
print("\n학습 완료 | 충전기", train["charger_pk"].nunique(), "대")

# ============================================================
# 8. 성능 평가 (AUC / Log Loss)  — 회귀 지표(MAE/RMSE)는 쓰지 않음
# ============================================================
prob = model.predict(X_test, num_iteration=model.best_iteration)

auc     = roc_auc_score(y_test, prob)
logloss = log_loss(y_test, np.clip(prob, 1e-3, 1-1e-3))
print(f"\n전체 구간")
print(f"  LightGBM   AUC {auc:.4f} | Log Loss {logloss:.4f}")

imp = (pd.DataFrame({"feature": features,
                     "importance": model.feature_importance(importance_type="gain")})
       .sort_values("importance", ascending=False).reset_index(drop=True))
print("상위 피처:", ", ".join(imp.head(6)["feature"]))

# ============================================================
# 9. 추천 로직 (혼잡 '확률' 기반)
# ============================================================
# 반경별 거리 가중치 / next-radius 폴백은 기존 로직 유지
RADIUS_OPTIONS = {1: 0.08, 3: 0.03, 5: 0.015}
NEXT_RADIUS    = {1: 3, 3: 5, 5: None}
MIN_CANDIDATES = 3
# 혼잡도 등급: '확률' 기준으로 재설정 (여유 < 0.25 ≤ 보통 < 0.5 ≤ 혼잡)
LEVELS = [(0.25, "여유"), (0.50, "보통"), (999, "혼잡")]

station_names = pd.read_csv("/content/station_names.csv")

# 서빙 스냅샷: 충전기별 최근 관측 1행
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

def recommend(date, hour, lat, lon, radius_km=3, top_n=3):
    if not 0 <= hour <= 23:
        raise ValueError("시간은 0~23 사이여야 합니다.")
    if radius_km not in RADIUS_OPTIONS:
        raise ValueError(f"반경은 {list(RADIUS_OPTIONS)} 중 하나여야 합니다.")

    distance_weight = RADIUS_OPTIONS[radius_km]
    target_time = pd.Timestamp(date) + pd.Timedelta(hours=hour)

    chargers = snapshot.copy()
    for col in categorical_cols:
        chargers[col] = chargers[col].astype("string")

    chargers["hour"]       = target_time.hour
    chargers["weekday"]    = target_time.dayofweek
    chargers["month"]      = target_time.month
    chargers["is_weekend"] = int(target_time.dayofweek >= 5)

    chargers["hist_charger_hour"] = (
        pd.MultiIndex.from_frame(chargers[["charger_pk", "hour"]]).map(hist_hour_map))
    chargers["hist_charger_weekday_hour"] = (
        pd.MultiIndex.from_frame(chargers[["charger_pk", "weekday", "hour"]]).map(hist_wh_map))

    for col in categorical_cols:
        chargers[col] = pd.Categorical(chargers[col], categories=category_maps[col])

    # ▶ 회귀 예측(predicted_usage) → 혼잡 확률(congestion_prob)로 변경
    chargers["congestion_prob"] = model.predict(
        chargers[features], num_iteration=model.best_iteration)

    # ── 충전기 → 충전소 집계 ──
    # 확률은 sum이 아니라 '평균'이 자연스럽다 (충전소의 평균 혼잡 확률)
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
    stations = stations[stations["distance_km"] <= radius_km]
    if stations.empty:
        nxt = NEXT_RADIUS[radius_km]
        msg = (f"{radius_km}km 안에 급속충전소가 없습니다. "
               f"가장 가까운 곳은 {min_dist:.1f}km 떨어져 있습니다.")
        if nxt and min_dist <= nxt:
            n_next = int((all_stations["distance_km"] <= nxt).sum())
            msg += f" {nxt}km까지 넓히면 {n_next}곳을 비교할 수 있습니다."
        return pd.DataFrame(), {"ok": False, "message": msg}

    # ── 순위: 혼잡 확률 + 거리 가중 (낮을수록 좋음) ──
    stations = stations.copy()
    stations["score"] = (stations["congestion_prob"]
                         + distance_weight * stations["distance_km"])

    top = stations.nsmallest(min(top_n, len(stations)), "score").reset_index(drop=True)
    top.insert(0, "순위", range(1, len(top) + 1))
    top["distance_km"]     = top["distance_km"].round(2)
    top["congestion_prob"] = top["congestion_prob"].round(4)

    table = top[["순위", "station_name", "혼잡도", "congestion_prob", "distance_km",
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
                    "congestion_prob": round(float(nearest["congestion_prob"]), 4),
                    "level": nearest["혼잡도"],
                    "chargers": int(nearest["charger_count"])},
        "items": [{"rank": int(r["순위"]), "name": r["station_name"],
                   "lat": float(r["위도"]), "lng": float(r["경도"]),
                   "distance_km": float(r["distance_km"]),
                   "congestion_prob": float(r["congestion_prob"]),
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
        print(payload["message"]); return
    display(table)
    n, b = payload["nearest"], payload["items"][0]
    print(f"반경 {radius_km}km 내 후보 {payload['n_candidates']}개소")
    print(f"가장 가까운 곳: {n['name']} {n['distance_km']}km "
          f"(충전기 {n['chargers']}대, {n['level']}, 혼잡확률 {n['congestion_prob']})")
    print(f"추천 1위     : {b['name']} {b['distance_km']}km "
          f"(충전기 {b['chargers']}대, {b['level']}, 혼잡확률 {b['congestion_prob']})")
    if "suggestion" in payload:
        print(payload["suggestion"])

# ============================================================
# 10. 입력 위젯 (기존과 동일)
# ============================================================
DATE, HOUR, LAT, LON, RADIUS = "2026-08-21", 15, 33.4996, 126.5312, 3
try:
    import ipywidgets as w
    from IPython.display import display as _d
    w_date = w.DatePicker(description="날짜", value=pd.Timestamp(DATE).date())
    w_hour = w.IntSlider(description="시간", min=0, max=23, value=HOUR)
    w_lat  = w.FloatText(description="위도", value=LAT, step=0.001)
    w_lon  = w.FloatText(description="경도", value=LON, step=0.001)
    w_rad  = w.ToggleButtons(description="반경", options=[("1km",1),("3km",3),("5km",5)], value=RADIUS)
    w_btn  = w.Button(description="추천 받기", button_style="primary")
    w_out  = w.Output()
    def _on_click(_):
        with w_out:
            w_out.clear_output()
            if w_date.value is None:
                print("날짜를 선택해주세요."); return
            show(str(w_date.value), w_hour.value, w_lat.value, w_lon.value, w_rad.value)
    w_btn.on_click(_on_click)
    _d(w.VBox([w.HBox([w_date, w_hour]), w.HBox([w_lat, w_lon]), w_rad, w_btn, w_out]))
    _on_click(None)
except Exception as e:
    print(f"위젯 사용 불가({type(e).__name__}) — 기본값으로 실행합니다.")
    show(DATE, HOUR, LAT, LON, RADIUS)
