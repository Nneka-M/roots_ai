# Dockerfile
FROM postgres:16

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    postgresql-server-dev-16 \
    flex \
    bison

# Clone and build Apache AGE
RUN git clone https://github.com/apache/age.git /age
WORKDIR /age
RUN git checkout PG16  # Use appropriate branch for your PG version

# Build AGE
RUN make install

# Install pgvector for embeddings
RUN git clone --branch v0.5.1 https://github.com/pgvector/pgvector.git /pgvector
WORKDIR /pgvector
RUN make && make install

# Configure PostgreSQL
COPY init.sql /docker-entrypoint-initdb.d/
COPY postgresql.conf /etc/postgresql/postgresql.conf

CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]