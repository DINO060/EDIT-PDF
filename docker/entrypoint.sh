#!/bin/bash
set -e

# Wait for services to be available
echo "Waiting for Redis..."
while ! nc -z redis 6379; do
  sleep 1
done
echo "Redis is available"

echo "Waiting for PostgreSQL..."
while ! nc -z postgres 5432; do
  sleep 1
done
echo "PostgreSQL is available"

# Run database migrations (if applicable)
if [ -f "migrations/migrate.py" ]; then
    echo "Running database migrations..."
    python migrations/migrate.py
fi

# Handle signals gracefully
_term() {
    echo "Caught SIGTERM signal!"
    kill -TERM "$child" 2>/dev/null
}

trap _term SIGTERM SIGINT

# Start the main process
echo "Starting PDF Bot..."
exec "$@" &
child=$!
wait "$child" 