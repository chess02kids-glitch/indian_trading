FROM python:3.12-slim
RUN useradd -m app && chown -R app /app
WORKDIR /app
COPY . .
USER app
HEALTHCHECK CMD python -c "print('ok')" || exit 1
ENTRYPOINT ["python","-m","cli"]
