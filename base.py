
from abc import ABC, abstractmethod

class TechnicalDataProvider(ABC):
    name = "base"

    @abstractmethod
    def configured(self) -> bool:
        ...

    @abstractmethod
    async def search_vehicle(self, **kwargs):
        ...

    @abstractmethod
    async def get_technical_data(self, vehicle_ref: str, section: str | None = None):
        ...

    @abstractmethod
    async def get_repair_procedure(self, vehicle_ref: str, procedure_ref: str):
        ...
