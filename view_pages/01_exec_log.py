"""네비게이션 대상 페이지 — 실행 로그."""
from dashboard_views.exec_log import render_exec_log_page
from web_common import get_tool

render_exec_log_page(get_tool())
