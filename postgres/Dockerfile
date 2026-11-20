# Postgres 16 (Alpine, the image ForgeSRE always used, so existing data directories keep their
# collation) with the pgvector extension for ForgeAI document search. Built once by install/update.
FROM postgres:16-alpine
ARG PGVECTOR_VERSION=v0.8.0
RUN apk add --no-cache --virtual .build-pgvector git build-base \
 && git clone --branch "$PGVECTOR_VERSION" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && cd /tmp/pgvector \
 && make with_llvm=no OPTFLAGS="" \
 && make install with_llvm=no \
 && cd / && rm -rf /tmp/pgvector \
 && apk del .build-pgvector
