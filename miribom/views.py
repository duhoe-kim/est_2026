import re
import requests

from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .services import *

# ══════════════════════════════════════════════════════════════════════
#  API 키 — 용도별로 분리해서 관리합니다
# ══════════════════════════════════════════════════════════════════════

# 지도 계열 (Web Dynamic Map · Geocoding · Directions 5) — 셋 다 같은 키를 씁니다
MAP_CLIENT_ID     = "fpdkq2mssw"
MAP_CLIENT_SECRET = "NdpmkSL4xtedHLL3vw1mVBahXZffgPzgKOI9aJE1"

# 지역검색 (Local Search) — 위와 다른 애플리케이션 키
LOCAL_SEARCH_CLIENT_ID     = "0q1xz5ekwz"
LOCAL_SEARCH_CLIENT_SECRET = "XLenLUq1LjCjDCgITVTPa54rndPcAquZLpxUBLjd"

# 예전 이름을 참조하는 코드가 있어도 깨지지 않게 남겨둡니다
CLIENT_ID = MAP_CLIENT_ID
CLIENT_SECRET = MAP_CLIENT_SECRET

# 세션 키
RESULT_KEY = "miribom_result"   # 예측 결과
FORM_KEY   = "miribom_form"     # 사용자가 입력했던 조건

DEFAULT_FORM = {
    "date": "", "time": "14:00", "dist": "3", "kw": "0",
    "loc": "", "lat": "", "lng": "",
}

TAG_RE = re.compile(r"<[^>]+>")

# 예측은 '정시' 단위로만 합니다 (모델 피처가 hour 라서 분은 쓰이지 않습니다).
# 그래서 시간 입력을 분 없이 시(時)만 고르는 목록으로 제공합니다.
HOUR_CHOICES = [{"value": f"{h:02d}:00", "label": f"{h:02d}시"} for h in range(24)]


def normalize_hour(value, default="14:00"):
    """'14:37', '14', 14 → '14:00' 으로 맞춘다."""
    try:
        h = int(str(value).split(":")[0])
    except (TypeError, ValueError):
        return default
    return f"{max(0, min(23, h)):02d}:00"


def get_form(request):
    """세션에 저장해 둔 입력 조건. 없으면 기본값."""
    form = dict(DEFAULT_FORM)
    form.update(request.session.get(FORM_KEY) or {})
    return form


def save_form(request, data):
    """입력 조건을 세션에 병합 저장한다 (페이지를 옮겨도 값이 유지되도록)."""
    form = get_form(request)
    for k in DEFAULT_FORM:
        if data.get(k) not in (None, ""):
            form[k] = data.get(k)
    form["time"] = normalize_hour(form.get("time"))   # 분은 버리고 정시로
    request.session[FORM_KEY] = form
    request.session.modified = True
    return form


# ══════════════════════════════════════════════════════════════════════
#  장소 검색
# ══════════════════════════════════════════════════════════════════════

def _in_korea(lat, lng):
    return lat is not None and lng is not None and 33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0


# 지역검색은 발급 경로에 따라 엔드포인트/헤더가 다릅니다.
# 어느 쪽이 되는지 첫 호출 때 확인하고, 그 뒤로는 되는 쪽만 씁니다.
LOCAL_SEARCH_ENDPOINTS = [
    ("https://openapi.naver.com/v1/search/local.json",
     {"X-Naver-Client-Id": LOCAL_SEARCH_CLIENT_ID,
      "X-Naver-Client-Secret": LOCAL_SEARCH_CLIENT_SECRET}),
    ("https://naveropenapi.apigw.ntruss.com/v1/search/local.json",
     {"x-ncp-apigw-api-key-id": LOCAL_SEARCH_CLIENT_ID,
      "x-ncp-apigw-api-key": LOCAL_SEARCH_CLIENT_SECRET}),
]
_local_endpoint = None      # 인증에 성공한 엔드포인트 인덱스
LAST_LOCAL_DEBUG = []       # 마지막 호출의 엔드포인트별 결과 (화면에 그대로 보여줍니다)


def local_search(query, display=5):
    """
    네이버 지역검색 API. 상호/기관 이름 검색용.
    mapx/mapy 는 WGS84 좌표 × 10^7 정수로 내려옵니다.
    반환: (items, error_message)
    """
    global _local_endpoint

    order = ([LOCAL_SEARCH_ENDPOINTS[_local_endpoint]] if _local_endpoint is not None
             else LOCAL_SEARCH_ENDPOINTS)

    data, last_err = None, None
    LAST_LOCAL_DEBUG.clear()

    for idx, (url, headers) in enumerate(order):
        host = url.split("/")[2]
        try:
            r = requests.get(url, headers=headers,
                             params={"query": query, "display": display, "sort": "random"},
                             timeout=10)
            LAST_LOCAL_DEBUG.append(f"{host} → HTTP {r.status_code} {r.text[:120]}")
            print(f"[local_search] {url} → {r.status_code} {r.text[:300]}")

            if r.status_code in (401, 403):
                last_err = (f"지역검색 키가 거부되었습니다 (HTTP {r.status_code}). "
                            f"developers.naver.com 의 '검색' API 키인지 확인하세요.")
                continue
            r.raise_for_status()
            data = r.json()
            _local_endpoint = idx if _local_endpoint is None else _local_endpoint
            break

        except requests.exceptions.RequestException as e:
            LAST_LOCAL_DEBUG.append(f"{host} → {type(e).__name__}: {str(e)[:120]}")
            last_err = f"지역검색 서버({host})에 연결하지 못했습니다."
            print(f"[local_search] {url} failed for {query}: {e!r}")
        except ValueError:
            LAST_LOCAL_DEBUG.append(f"{host} → JSON 파싱 실패")
            last_err = "지역검색 응답을 해석하지 못했습니다."

    if data is None:
        return [], last_err or "지역검색에 실패했습니다."

    items = []
    for it in data.get("items", []):
        lat = lng = None
        try:
            lng = int(it["mapx"]) / 1e7
            lat = int(it["mapy"]) / 1e7
        except (KeyError, ValueError, TypeError):
            pass
        if not _in_korea(lat, lng):
            lat = lng = None

        items.append({
            "name": TAG_RE.sub("", it.get("title", "")),
            "address": it.get("roadAddress") or it.get("address") or "",
            "category": it.get("category", ""),
            "lat": lat, "lng": lng,
            "source": "지역검색",
        })
    return items, None


def geocode_search(query, count=5):
    """
    NCP 지오코딩 API. 도로명/지번 주소 검색용. 후보를 여러 개 돌려줍니다.
    반환: (items, error_message)
    """
    url = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
    headers = {
        "x-ncp-apigw-api-key-id": MAP_CLIENT_ID,
        "x-ncp-apigw-api-key": MAP_CLIENT_SECRET,
    }
    try:
        r = requests.get(url, headers=headers,
                         params={"query": query, "count": count}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        print(f"[geocode_search] failed for {query}: {e}")
        return [], "주소 검색 서버에 연결하지 못했습니다."
    except ValueError:
        return [], "주소 검색 응답을 해석하지 못했습니다."

    items = []
    for a in data.get("addresses", []):
        try:
            lng, lat = float(a["x"]), float(a["y"])
        except (KeyError, ValueError, TypeError):
            continue
        if not _in_korea(lat, lng):
            continue
        road = a.get("roadAddress") or ""
        jibun = a.get("jibunAddress") or ""
        items.append({
            "name": road or jibun,
            "address": jibun if road else road,
            "category": "",
            "lat": lat, "lng": lng,
            "source": "주소",
        })
    return items, None


def _relaxed_queries(query):
    """'제주 캠퍼스' 처럼 주소가 아닌 말은 지오코딩이 0건을 냅니다.
    단어를 하나씩 떼어내며 다시 시도할 후보 문자열을 만듭니다."""
    tokens = query.split()
    out = []
    if len(tokens) > 1:
        out.append("".join(tokens))          # 붙여쓰기
        for i in range(len(tokens) - 1, 0, -1):
            out.append(" ".join(tokens[:i]))  # 뒤에서부터 한 단어씩 제거
        out.extend(tokens)                    # 개별 단어
    return out


def search_place(request):
    """
    장소 검색 JSON API.
    ① 지역검색(상호·기관)  ② 지오코딩(주소)  ③ 내장 충전소 이름
    순으로 모아서, 최소 한 건이라도 나오도록 단계적으로 넓힙니다.
    """
    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse({"ok": False, "message": "검색어를 입력해 주세요.", "items": []})

    local_items, local_err = local_search(query)
    geo_items, geo_err = geocode_search(query)

    merged, seen = [], set()

    def add(items):
        for it in items:
            if it.get("lat") is None:
                continue
            key = (round(it["lat"], 4), round(it["lng"], 4))
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)

    add(local_items)
    add(geo_items)

    # ③ 외부 API가 막혔거나 0건이면 내장 충전소 이름으로 찾는다
    fallback_used = False
    if not merged:
        station_hits = search_station_names(query)
        if station_hits:
            fallback_used = True
            add(station_hits)

    # ④ 그래도 없으면 검색어를 줄여가며 재시도
    relaxed_with = None
    if not merged:
        for alt in _relaxed_queries(query):
            alt_local, _ = local_search(alt)
            alt_geo, _ = geocode_search(alt)
            alt_station = search_station_names(alt)
            if alt_local or alt_geo or alt_station:
                relaxed_with = alt
                fallback_used = fallback_used or bool(alt_station and not (alt_local or alt_geo))
                add(alt_local); add(alt_geo); add(alt_station)
                break

    notice_parts = []
    if not local_items and local_err:
        notice_parts.append(local_err)
    if fallback_used:
        notice_parts.append("외부 검색이 비어 있어 내장 충전소 데이터에서 찾았습니다.")
    if relaxed_with:
        notice_parts.append(f"‘{relaxed_with}’ 로 넓혀 검색했습니다.")

    return JsonResponse({
        "ok": bool(merged),
        "query": query,
        "items": merged,
        "notice": " ".join(notice_parts) or None,
        "debug": LAST_LOCAL_DEBUG[:] if not local_items else None,
        "message": None if merged else (
            f"'{query}' 에 해당하는 장소를 찾지 못했습니다. "
            + (local_err or geo_err or "다른 이름이나 주소로 검색해 보세요.")),
    })


NEXT_KEY = "miribom_next"       # 장소 검색이 끝나면 돌아갈 화면


def _safe_next(request, raw):
    """열린 리다이렉트 방지 — 우리 사이트 안의 경로일 때만 허용한다."""
    if raw and url_has_allowed_host_and_scheme(
            raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return raw
    return None


def search(request):
    """
    장소 검색 화면.
    넘어온 조건은 세션에 저장해 두고, 어느 화면에서 왔는지(next)도 함께 기억해
    ‘이 장소로 계속하기’ 를 누르면 그 화면으로 되돌아갑니다.
    """
    if request.method == "POST":
        save_form(request, request.POST)

    raw_next = request.POST.get("next") or request.GET.get("next") or ""
    nxt = _safe_next(request, raw_next)
    if nxt:
        request.session[NEXT_KEY] = nxt
    else:
        nxt = _safe_next(request, request.session.get(NEXT_KEY, ""))

    return render(request, "search.html", {
        "form": get_form(request),
        "next": nxt or reverse("home"),
        "map_client_id": MAP_CLIENT_ID,
    })


def select_place(request):
    """검색 결과에서 고른 장소를 세션에 저장하고, 왔던 화면으로 되돌아간다."""
    if request.method != "POST":
        return redirect("home")

    save_form(request, {
        "loc": request.POST.get("loc"),
        "lat": request.POST.get("lat"),
        "lng": request.POST.get("lng"),
    })

    nxt = (_safe_next(request, request.POST.get("next", ""))
           or _safe_next(request, request.session.get(NEXT_KEY, "")))
    request.session.pop(NEXT_KEY, None)

    return redirect(nxt or reverse("home"))


# ══════════════════════════════════════════════════════════════════════
#  예측 흐름
# ══════════════════════════════════════════════════════════════════════

def geocode(address):
    """주소 문자열 → (경도, 위도). 실패하면 (None, None)."""
    items, _ = geocode_search(address, count=1)
    if not items:
        items, _ = local_search(address, display=1)
        items = [i for i in items if i["lat"] is not None]
    if not items:
        return None, None
    return items[0]["lng"], items[0]["lat"]


def homepage(request):
    return render(request, "home.html",
                  {"form": get_form(request), "hours": HOUR_CHOICES})


def predict(request):
    """로딩 화면. 조건을 템플릿에 심어두면 JS가 /get_result/ 를 호출한다."""
    if request.method != "POST":
        return render(request, "home.html",
                      {"form": get_form(request), "hours": HOUR_CHOICES})

    form = save_form(request, request.POST)
    address = form["loc"] or "제주국제공항"

    # 검색 화면에서 좌표까지 골라 왔으면 지오코딩을 건너뛴다
    try:
        longitude, latitude = float(form["lng"]), float(form["lat"])
    except (TypeError, ValueError):
        longitude, latitude = geocode(address)

    if longitude is None:
        return render(request, "home.html", {
            "form": form, "hours": HOUR_CHOICES,
            "error": f"'{address}' 의 위치를 찾지 못했습니다. ‘장소 찾기’로 검색해 주세요.",
        })

    time = form["time"] or "14:00"

    return render(request, "predict.html", {
        "date": form["date"],
        "time": int(str(time).split(":")[0]),
        "time_label": time,
        "dist": form["dist"] or 3,
        "kw": form["kw"] or 0,
        "loc": address,
        "x": longitude,
        "y": latitude,
    })


def get_result(request):
    """예측을 실행하고 결과를 세션에 저장한다. 응답으로 이동할 URL을 알려준다."""
    date = request.GET.get("date")
    time = request.GET.get("time")
    dist = request.GET.get("dist")
    kw   = request.GET.get("kw")
    lon  = request.GET.get("lon")
    lat  = request.GET.get("lat")
    loc  = request.GET.get("loc", "")

    try:
        outputs = make_prediction(date, time, dist, kw, lon, lat)
    except Exception as e:
        print(f"[get_result] prediction failed: {e!r}")
        return JsonResponse(
            {"ok": False, "message": f"예측 중 오류가 발생했습니다: {e}"}, status=500
        )

    outputs["cond"] = {"date": date, "time": time, "dist": dist, "kw": kw, "loc": loc}

    request.session[RESULT_KEY] = outputs
    request.session.modified = True

    return JsonResponse({**outputs, "redirect": reverse("result")})


def result(request):
    """세션에 담긴 예측 결과를 렌더링한다."""
    payload = request.session.get(RESULT_KEY)
    if not payload:
        return redirect("home")

    return render(request, "result.html", {
        "payload": payload,
        "form": get_form(request),
        "hours": HOUR_CHOICES,
        "map_client_id": MAP_CLIENT_ID,
    })


def detail(request):
    """추천 충전소 상세 — 경로와 충전소 정보를 함께 보여준다."""
    payload = request.session.get(RESULT_KEY)
    pick = request.GET.get("pick", "")

    if not payload:
        return redirect("home")

    items = payload.get("items", [])
    item = next((i for i in items if i["name"] == pick), None) or (items[0] if items else None)

    if item is None:
        return redirect("result")

    return render(request, "detail.html", {
        "payload": payload,
        "item": item,
        "map_client_id": MAP_CLIENT_ID,
    })


# ══════════════════════════════════════════════════════════════════════
#  길찾기 (Directions 5)
# ══════════════════════════════════════════════════════════════════════

# 응답의 route 안에 들어오는 키 (요청한 option 과 같은 이름)
ROUTE_OPTIONS = ["trafast", "traoptimal", "tracomfort"]


def driving_route(start, goal, option="trafast"):
    """
    NCP Directions 5. start/goal 은 "경도,위도" 문자열.
    반환: (result_dict, error_message)
    """
    url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
    headers = {
        "x-ncp-apigw-api-key-id": MAP_CLIENT_ID,
        "x-ncp-apigw-api-key": MAP_CLIENT_SECRET,
    }
    try:
        r = requests.get(url, headers=headers,
                         params={"start": start, "goal": goal, "option": option},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        print(f"[driving_route] failed {start}->{goal}: {e}")
        return None, "길찾기 서버에 연결하지 못했습니다."
    except ValueError:
        return None, "길찾기 응답을 해석하지 못했습니다."

    # code 0 이 아니면 경로를 못 찾은 것 (1 = 출발지와 도착지가 너무 가까움)
    if data.get("code") != 0:
        return None, data.get("message") or "경로를 찾지 못했습니다."

    route = data.get("route") or {}
    leg = None
    for key in [option] + ROUTE_OPTIONS:
        if route.get(key):
            leg = route[key][0]
            break
    if not leg:
        return None, "경로 데이터가 비어 있습니다."

    s = leg.get("summary", {})
    return {
        "path": leg.get("path", []),                      # [[경도, 위도], …]
        "distance_m": s.get("distance"),
        "duration_ms": s.get("duration"),
        "toll_fare": s.get("tollFare"),
        "fuel_price": s.get("fuelPrice"),
        "taxi_fare": s.get("taxiFare"),
        "bbox": s.get("bbox"),
    }, None


def route_api(request):
    """/api/route/?slat=&slng=&glat=&glng=  →  경로 좌표 + 요약"""
    try:
        slat, slng = float(request.GET["slat"]), float(request.GET["slng"])
        glat, glng = float(request.GET["glat"]), float(request.GET["glng"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "좌표가 올바르지 않습니다."}, status=400)

    option = request.GET.get("option", "trafast")
    if option not in ROUTE_OPTIONS:
        option = "trafast"

    data, err = driving_route(f"{slng},{slat}", f"{glng},{glat}", option)
    if err:
        return JsonResponse({"ok": False, "message": err})

    return JsonResponse({"ok": True, **data})
