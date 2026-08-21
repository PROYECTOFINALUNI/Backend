FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system django \
    && adduser --system --ingroup django --home /app django

COPY pyproject.toml ./
COPY common ./common
COPY config ./config
COPY users ./users
COPY expenses ./expenses
COPY approvals ./approvals
COPY manage.py ./
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

RUN pip install --upgrade "pip==25.1.1" \
    && pip install . \
    && DJANGO_SETTINGS_MODULE=config.settings.dev python manage.py collectstatic --noinput

USER django

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
