"""
core/services.py
Service lifecycle management and task registry with TTL cleanup
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional
from threading import Lock

logger = logging.getLogger("GodNode.Services")

class BoundedTaskRegistry:
    """Task registry with TTL-based cleanup and size limits"""
    
    def __init__(self, max_size: int = 10000, ttl_seconds: int = 3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def add_task(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """Add or update a task"""
        with self._lock:
            if len(self._registry) >= self.max_size:
                logger.warning(f"Task registry at capacity ({self.max_size}). Oldest entries may be evicted.")
                self._evict_oldest()
            
            task_data['created_at'] = time.time()
            task_data['updated_at'] = time.time()
            self._registry[task_id] = task_data
    
    def update_task(self, task_id: str, updates: Dict[str, Any]) -> None:
        """Update an existing task"""
        with self._lock:
            if task_id in self._registry:
                self._registry[task_id].update(updates)
                self._registry[task_id]['updated_at'] = time.time()
    
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a task by ID"""
        with self._lock:
            return self._registry.get(task_id)
    
    def _evict_oldest(self) -> None:
        """Remove oldest tasks to make space (internal, must hold lock)"""
        if not self._registry:
            return
        
        # Find and remove completed/failed tasks first
        to_remove = []
        for task_id, task in self._registry.items():
            if task.get('status') in ['SUCCESS', 'FAILED']:
                to_remove.append(task_id)
                if len(to_remove) >= max(10, self.max_size // 10):
                    break
        
        if to_remove:
            for task_id in to_remove:
                del self._registry[task_id]
                logger.debug(f"Evicted completed task {task_id}")
    
    async def cleanup_expired(self) -> int:
        """Remove expired tasks (runs in background)"""
        now = time.time()
        removed = 0
        
        with self._lock:
            expired_ids = [
                task_id for task_id, task in self._registry.items()
                if now - task.get('created_at', now) > self.ttl_seconds
                and task.get('status') in ['SUCCESS', 'FAILED']
            ]
            
            for task_id in expired_ids:
                del self._registry[task_id]
                removed += 1
        
        if removed > 0:
            logger.debug(f"Cleaned up {removed} expired tasks")
        
        return removed
    
    async def start_cleanup_service(self, interval_seconds: int = 300) -> None:
        """Start background cleanup task"""
        self._cleanup_task = asyncio.current_task()
        logger.info(f"Task cleanup service started (interval: {interval_seconds}s)")
        
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.cleanup_expired()
        except asyncio.CancelledError:
            logger.info("Task cleanup service stopped")
    
    def stop_cleanup_service(self) -> None:
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()


class ServiceRegistry:
    """Registry for managing service state and readiness"""
    
    def __init__(self):
        self._services: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
    
    def register(self, name: str, status: str, details: Optional[Dict] = None) -> None:
        """Register or update a service"""
        with self._lock:
            self._services[name] = {
                'name': name,
                'status': status,
                'details': details or {},
                'timestamp': time.time()
            }
    
    def get_status(self, name: str) -> Optional[str]:
        """Get service status"""
        with self._lock:
            service = self._services.get(name)
            return service['status'] if service else None
    
    def get_readiness(self) -> Dict[str, Any]:
        """Get overall system readiness status"""
        with self._lock:
            critical_services = ['config', 'gateway', 'scheduler', 'http_client']
            ready = all(
                self._services.get(svc, {}).get('status') == 'READY'
                for svc in critical_services
            )
            
            return {
                'ready': ready,
                'services': {name: svc['status'] for name, svc in self._services.items()},
                'timestamp': time.time()
            }


# Global instances
task_registry = BoundedTaskRegistry()
service_registry = ServiceRegistry()
