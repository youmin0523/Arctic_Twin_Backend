"""
db.py
=====
report-service 가 Neon PostgreSQL 정형 데이터를 조회하기 위한 psycopg2 헬퍼.

DATABASE_URL 환경변수(backend/.env, server.py 가 load_dotenv 로 로드)를 사용한다.
URL 미설정 또는 연결 실패 시 호출측이 기존 JSON 파일로 폴백하도록
db_available() / fetch_all() 을 제공한다.
"""

import logging
import os

logger = logging.getLogger("report-service.db")

try:
    import psycopg2
    import psycopg2.extras
    _HAS_PSYCOPG = True
except Exception:  # pragma: no cover - 패키지 미설치 환경
    psycopg2 = None
    _HAS_PSYCOPG = False
    logger.warning("psycopg2 미설치 — DB 조회 불가, JSON 파일 폴백으로 동작")


def db_available() -> bool:
    """DATABASE_URL 이 설정되어 있고 psycopg2 를 쓸 수 있는지."""
    return _HAS_PSYCOPG and bool(os.environ.get("DATABASE_URL"))


def fetch_all(sql: str, params: tuple | None = None) -> list[dict]:
    """SELECT 결과를 dict 리스트로 반환. 실패 시 예외를 던져 호출측이 폴백하도록 함.

    Neon 은 SSL 필수이나 DATABASE_URL 에 sslmode=require 가 포함되어 있어
    별도 ssl 옵션 없이 연결된다.
    """
    if not db_available():
        raise RuntimeError("DATABASE_URL 미설정 또는 psycopg2 없음")

    conn = None
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        if conn is not None:
            conn.close()
