#!/bin/bash

# Check if the bot process is running
if ! pgrep -f "python main.py" > /dev/null; then
    echo "Bot process not running"
    exit 1
fi

# Check Redis connection (if available)
if command -v redis-cli > /dev/null; then
    if ! redis-cli -h redis ping > /dev/null 2>&1; then
        echo "Cannot connect to Redis"
        exit 1
    fi
fi

# Check if temp directory is writable
if [ ! -w "/app/data/temp" ]; then
    echo "Temp directory not writable"
    exit 1
fi

# Check available disk space (at least 100MB)
available_space=$(df /app/data | tail -1 | awk '{print $4}')
if [ "$available_space" -lt 102400 ]; then
    echo "Low disk space: ${available_space}KB available"
    exit 1
fi

echo "Health check passed"
exit 0 