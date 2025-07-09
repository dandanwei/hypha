# Redis Performance Optimization Fixes for Hypha

## Fix 1: Add Connection Pool Configuration

```python
# Modified hypha/core/store.py - Redis initialization with connection pool
import aioredis
from aioredis import ConnectionPool

class RedisStore:
    def __init__(self, app, server_id=None, public_base_url=None, 
                 local_base_url=None, redis_uri=None, database_uri=None,
                 ollama_host=None, openai_config=None, cache_dir=None,
                 enable_service_search=False, reconnection_token_life_time=2 * 24 * 60 * 60,
                 activity_check_interval=10):
        # ... existing initialization code ...
        
        if redis_uri and redis_uri.startswith("redis://"):
            # Configure connection pool for better performance
            self._redis = aioredis.from_url(
                redis_uri,
                max_connections=50,  # Increase from default
                retry_on_timeout=True,
                health_check_interval=30,
                socket_keepalive=True,
                socket_keepalive_options={},
                encoding="utf-8",
                decode_responses=False,  # Keep as bytes for pub/sub
            )
        else:
            # Create a redis server with fakeredis
            from fakeredis import aioredis
            self._redis = aioredis.FakeRedis.from_url("redis://localhost:9997/11")
```

## Fix 2: Optimize Message Processing with Adaptive Concurrency

```python
# Modified hypha/core/__init__.py - RedisEventBus class
import os
import asyncio
import time
import json
import msgpack  # Already in requirements.txt

class RedisEventBus:
    def __init__(self, redis) -> None:
        """Initialize the event bus."""
        self._redis = redis
        self._handle_connected = None
        self._stop = False
        self._local_event_bus = EventBus(logger)
        self._redis_event_bus = EventBus(logger)
        
        # Optimized health check interval
        self._health_check_interval = 30  # Reduced from 5 seconds
        
        # Adaptive concurrency based on CPU cores
        cpu_count = os.cpu_count() or 1
        self._max_concurrent_tasks = min(cpu_count * 20, 100)  # Higher limit with cap
        self._processing_semaphore = asyncio.Semaphore(self._max_concurrent_tasks)
        
        # Performance tracking
        self._message_count = 0
        self._last_adaptation_time = time.time()
        self._adaptation_interval = 60  # Adapt every minute
        
        # ... rest of initialization ...

    async def _subscribe_redis(self):
        """Handle Redis subscription with optimized processing."""
        logger.info(f"Starting Redis event bus with {self._max_concurrent_tasks} concurrent task processing")

        while not self._stop:
            try:
                pubsub = self._redis.pubsub()
                self._stop = False

                # Subscribe to both events and health check channel
                await pubsub.psubscribe("event:*")
                await pubsub.subscribe(self._health_check_channel)

                self._ready.set_result(True) if not self._ready.done() else None
                self._counter.labels(event="subscription", status="success").inc()

                while not self._stop:
                    if self._circuit_breaker_open:
                        await asyncio.sleep(0.1)
                        continue

                    try:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=0.05
                        )
                        if msg:
                            channel = msg["channel"].decode("utf-8")
                            if channel == self._health_check_channel:
                                await self._process_health_check_message(msg["data"])
                            else:
                                # Use adaptive semaphore for message processing
                                task = asyncio.create_task(
                                    self._process_message_optimized(msg)
                                )
                                background_tasks.add(task)
                                task.add_done_callback(background_tasks.discard)
                                
                                # Adaptive concurrency adjustment
                                await self._adapt_concurrency()
                                
                    except Exception as e:
                        logger.warning(f"Error getting message: {str(e)}")
                        self._counter.labels(event="message_processing", status="failure").inc()
                        await asyncio.sleep(0.1)

            except Exception as exp:
                logger.error(f"Subscription error: {str(exp)}")
                self._counter.labels(event="subscription", status="failure").inc()
                if not self._ready.done():
                    self._ready.set_exception(exp)
                await asyncio.sleep(1)

    async def _process_message_optimized(self, msg):
        """Optimized message processing with better serialization."""
        async with self._processing_semaphore:
            try:
                channel = msg["channel"].decode("utf-8")
                RedisEventBus._counter.labels(event="*", status="processed").inc()
                self._message_count += 1

                if channel.startswith("event:b:"):
                    event_type = channel[8:]
                    data = msg["data"]
                    await self._redis_event_bus.emit(event_type, data)
                    
                elif channel.startswith("event:d:"):
                    event_type = channel[8:]
                    # Use msgpack for faster serialization when possible
                    try:
                        data = msgpack.unpackb(msg["data"], raw=False)
                    except Exception:
                        # Fallback to JSON if msgpack fails
                        data = json.loads(msg["data"])
                    await self._redis_event_bus.emit(event_type, data)
                    
                elif channel.startswith("event:s:"):
                    event_type = channel[8:]
                    data = msg["data"].decode("utf-8")
                    await self._redis_event_bus.emit(event_type, data)
                    
                else:
                    logger.info("Unknown channel: %s", channel)
                    
                if ":" not in event_type:
                    RedisEventBus._counter.labels(event=event_type, status="processed").inc()
                    
            except Exception as exp:
                logger.exception(f"Error processing message: {exp}")
                RedisEventBus._counter.labels(event="message_processing", status="error").inc()

    async def _adapt_concurrency(self):
        """Adapt concurrency based on message processing performance."""
        current_time = time.time()
        if current_time - self._last_adaptation_time < self._adaptation_interval:
            return
            
        # Calculate processing rate
        time_diff = current_time - self._last_adaptation_time
        messages_per_second = self._message_count / time_diff if time_diff > 0 else 0
        
        # Adjust semaphore based on load
        if messages_per_second > 1000:  # High load
            new_limit = min(self._max_concurrent_tasks + 10, 200)
        elif messages_per_second < 100:  # Low load
            new_limit = max(self._max_concurrent_tasks - 5, 20)
        else:
            new_limit = self._max_concurrent_tasks
            
        if new_limit != self._max_concurrent_tasks:
            self._max_concurrent_tasks = new_limit
            # Create new semaphore with updated limit
            old_semaphore = self._processing_semaphore
            self._processing_semaphore = asyncio.Semaphore(new_limit)
            logger.info(f"Adapted concurrency limit to {new_limit} (messages/sec: {messages_per_second:.1f})")
            
        self._message_count = 0
        self._last_adaptation_time = current_time
```

## Fix 3: Optimize Redis Configuration in Deployment

```yaml
# Modified helm-charts/redis/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  labels:
    app: redis
    app.kubernetes.io/managed-by: "Helm"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      securityContext:
        fsGroup: 1001
        runAsNonRoot: true
        runAsUser: 1001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: redis
          image: redis:7.2-alpine  # Updated to newer version
          command:
            - "sh"
            - "-c"
            - |
              redis-server \
                --dir /data \
                --port 6379 \
                --bind 0.0.0.0 \
                --appendonly yes \
                --protected-mode no \
                --pidfile /data/redis-6379.pid \
                --tcp-keepalive 300 \
                --timeout 0 \
                --tcp-backlog 511 \
                --maxclients 10000 \
                --save "" \
                --stop-writes-on-bgsave-error no \
                --lua-time-limit 5000 \
                --slowlog-log-slower-than 10000 \
                --notify-keyspace-events "" \
                --client-output-buffer-limit pubsub 256mb 128mb 60
          ports:
            - containerPort: 6379
          volumeMounts:
            - name: redis-data
              mountPath: /data
          resources:
            limits:
              cpu: "4"      # Increased from 2
              memory: "8Gi" # Increased from 4Gi
            requests:
              cpu: "2"      # Increased from 1
              memory: "4Gi" # Increased from 3Gi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            runAsNonRoot: true
            runAsUser: 1001
            seccompProfile:
              type: RuntimeDefault
      volumes:
        - name: redis-data
          emptyDir: {}
```

## Fix 4: Enable Redis in Production Deployment

```yaml
# Modified helm-charts/hypha-server/values.yaml
# ... existing configuration ...

env:
  - name: HYPHA_JWT_SECRET
    valueFrom:
      secretKeyRef:
        name: hypha-secrets
        key: HYPHA_JWT_SECRET
  - name: HYPHA_HOST
    value: "0.0.0.0"
  - name: HYPHA_PORT
    value: "9520"
  - name: HYPHA_PUBLIC_BASE_URL
    value: "https://hypha.amun.ai"
  - name: HYPHA_DATABASE_URI
    value: "sqlite+aiosqlite:////data/hypha-app-database.db"
  # Enable Redis for scaling
  - name: HYPHA_REDIS_URI
    value: "redis://redis.hypha.svc.cluster.local:6379/0"
  - name: HYPHA_SERVER_ID
    valueFrom:
      fieldRef:
        fieldPath: metadata.uid

# Define command-line arguments here
startupCommand:
  command: ["python", "-m", "uvicorn"]
  args:
    - "hypha.server:app"
    - "--host=$(HYPHA_HOST)"
    - "--port=$(HYPHA_PORT)"
    - "--redis-uri=$(HYPHA_REDIS_URI)"
```

## Fix 5: Add Performance Monitoring

```python
# Add to hypha/core/__init__.py - RedisEventBus class
from prometheus_client import Gauge, Counter, Histogram

class RedisEventBus:
    _counter = Counter(
        "event_bus", "Counts the events on the redis event bus", ["event", "status"]
    )
    _pubsub_latency = Gauge(
        "redis_pubsub_latency_seconds", "Redis pubsub latency in seconds"
    )
    # Add new metrics
    _connection_pool_size = Gauge(
        "redis_connection_pool_size", "Number of connections in pool"
    )
    _connection_pool_available = Gauge(
        "redis_connection_pool_available", "Number of available connections"
    )
    _message_processing_time = Histogram(
        "redis_message_processing_seconds", "Time to process Redis messages"
    )
    _concurrent_tasks = Gauge(
        "redis_concurrent_tasks", "Number of concurrent message processing tasks"
    )

    def __init__(self, redis) -> None:
        # ... existing initialization ...
        
        # Update metrics periodically
        self._metrics_update_task = None
        self._start_metrics_update()

    def _start_metrics_update(self):
        """Start periodic metrics update."""
        async def update_metrics():
            while not self._stop:
                try:
                    # Update connection pool metrics if available
                    if hasattr(self._redis, 'connection_pool'):
                        pool = self._redis.connection_pool
                        self._connection_pool_size.set(pool._created_connections)
                        self._connection_pool_available.set(pool._available_connections)
                    
                    # Update concurrent tasks metric
                    self._concurrent_tasks.set(self._max_concurrent_tasks - self._processing_semaphore._value)
                    
                    await asyncio.sleep(10)  # Update every 10 seconds
                except Exception as e:
                    logger.warning(f"Error updating metrics: {e}")
                    await asyncio.sleep(30)
        
        self._metrics_update_task = asyncio.create_task(update_metrics())

    async def _process_message_optimized(self, msg):
        """Optimized message processing with timing metrics."""
        start_time = time.time()
        async with self._processing_semaphore:
            try:
                # ... existing processing code ...
                
                # Record processing time
                processing_time = time.time() - start_time
                self._message_processing_time.observe(processing_time)
                
            except Exception as exp:
                logger.exception(f"Error processing message: {exp}")
                self._counter.labels(event="message_processing", status="error").inc()
```

## Fix 6: WebSocket Connection Optimization

```python
# Modified hypha/websocket.py - WebsocketServer class
import asyncio
from typing import Dict, Set

class WebsocketServer:
    def __init__(self, store: RedisStore, path="/ws"):
        """Initialize websocket server with the store and set up the endpoint."""
        self.store = store
        app = store._app
        self.store.set_websocket_server(self)
        self._stop = False
        self._websockets = {}
        
        # Add connection pooling for better performance
        self._connection_pool = {}
        self._active_connections = 0
        self._max_connections = 1000  # Configurable limit
        
        # Performance tracking
        self._connection_metrics = {
            'total_connections': 0,
            'active_connections': 0,
            'messages_sent': 0,
            'messages_received': 0,
        }

    async def establish_websocket_communication(
        self, websocket, workspace, client_id, user_info
    ):
        """Establish websocket communication with optimized connection management."""
        
        # Check connection limits
        if self._active_connections >= self._max_connections:
            logger.warning(f"Connection limit reached ({self._max_connections})")
            await websocket.close(code=1008, reason="Connection limit exceeded")
            return
            
        # Track connection
        connection_key = f"{workspace}/{client_id}"
        self._websockets[connection_key] = websocket
        self._active_connections += 1
        self._connection_metrics['total_connections'] += 1
        self._connection_metrics['active_connections'] = self._active_connections
        
        logger.info(f"WebSocket connected: {connection_key} (active: {self._active_connections})")
        
        # Create Redis RPC connection with optimized settings
        event_bus = self.store.get_event_bus()
        conn = RedisRPCConnection(event_bus, workspace, client_id, user_info, None)
        
        # Set up connection with timeout and retry logic
        await self._setup_connection_with_retry(conn, connection_key)
        
        # ... rest of the communication logic ...

    async def _setup_connection_with_retry(self, conn, connection_key, max_retries=3):
        """Setup connection with retry logic for better reliability."""
        for attempt in range(max_retries):
            try:
                await conn.connect()
                logger.info(f"Redis RPC connection established: {connection_key}")
                return
            except Exception as e:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Failed to establish connection after {max_retries} attempts")
                    raise

    async def handle_disconnection(
        self, websocket, workspace: str, client_id: str, user_info: UserInfo, code, exp
    ):
        """Handle client disconnection with optimized cleanup."""
        connection_key = f"{workspace}/{client_id}"
        
        # Update metrics
        if connection_key in self._websockets:
            self._active_connections = max(0, self._active_connections - 1)
            self._connection_metrics['active_connections'] = self._active_connections
            del self._websockets[connection_key]
            
        logger.info(f"WebSocket disconnected: {connection_key} (active: {self._active_connections})")
        
        # ... rest of disconnection logic ...
```

## Implementation Steps

1. **Apply Fix 1**: Update `hypha/core/store.py` with connection pool configuration
2. **Apply Fix 2**: Update `hypha/core/__init__.py` with optimized message processing
3. **Apply Fix 3**: Update Redis deployment configuration
4. **Apply Fix 4**: Enable Redis in production deployment
5. **Apply Fix 5**: Add comprehensive performance monitoring
6. **Apply Fix 6**: Optimize WebSocket connection management

## Testing the Fixes

```bash
# Test Redis connection pool
curl -X GET "http://localhost:9520/metrics" | grep redis

# Test pub/sub latency
curl -X GET "http://localhost:9520/health/readiness"

# Load test with multiple WebSocket connections
# (Use a WebSocket testing tool to simulate multiple clients)
```

These fixes should significantly improve the Redis pub/sub performance in the Hypha system.