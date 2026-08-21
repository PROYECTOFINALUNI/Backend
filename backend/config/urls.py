from django.contrib import admin
from django.db import connection
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from approvals.api import ApprovalRuleViewSet
from expenses.api import CategoryViewSet, ExpenseViewSet, ReportViewSet, WarningRuleViewSet
from users.api import OrgAttributeViewSet, UserViewSet, login_view, logout_view, me


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except OperationalError:
        return JsonResponse(
            {
                "code": "database_unavailable",
                "detail": "Database health check failed.",
                "fields": {},
            },
            status=503,
        )
    return JsonResponse({"status": "ok"})


router = DefaultRouter()
router.register("users", UserViewSet)
router.register("org-attributes", OrgAttributeViewSet)
router.register("categories", CategoryViewSet)
router.register("warning-rules", WarningRuleViewSet)
router.register("approval-rules", ApprovalRuleViewSet)
router.register("expenses", ExpenseViewSet)
router.register("reports", ReportViewSet)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/login/", login_view),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/auth/logout/", logout_view),
    path("api/me/", me),
    path("api/health/", health),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("api/", include(router.urls)),
]
