import importlib

from src.throttles.base_throttle import BaseThrottle


class ThrottleFactory:
    @staticmethod
    def get_instance(context_manager, throttle_type, throttle_config) -> BaseThrottle:
        module = importlib.import_module(f"src.throttles.{throttle_type}_throttle")
        throttle_class = getattr(
            module, throttle_type.title().replace("_", "") + "Throttle"
        )
        return throttle_class(context_manager, throttle_type, throttle_config)
