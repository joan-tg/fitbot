FROM python:3.13

ENV PYTHONPATH=src
WORKDIR /app

COPY src /app/src
COPY api /app/api
COPY tests /app/tests
COPY Makefile pyproject.toml uv.lock /app/

RUN pip install uv

CMD ["make", "run"]
