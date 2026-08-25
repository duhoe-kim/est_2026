"""
URL configuration for miribom project.
"""
from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.homepage, name="home"),

    # 장소 검색
    path('search/', views.search, name="search"),
    path('search/select/', views.select_place, name="select_place"),
    path('api/search-place/', views.search_place, name="search_place"),
    path('api/route/', views.route_api, name="route_api"),

    # 예측
    path('predict/', views.predict, name="predict"),
    path('get_result/', views.get_result, name="get_result"),
    path('result/', views.result, name="result"),
    path('detail/', views.detail, name="detail"),
]
