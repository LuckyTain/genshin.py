from __future__ import annotations

import asyncio
import heapq
from typing import *

from .models import ClaimedDailyReward, ItemTransaction, Transaction, Wish
from .utils import aislice, amerge

if TYPE_CHECKING:
    from .client import GenshinClient


class IDModel(Protocol):
    id: int


IDModelT = TypeVar("IDModelT", bound=IDModel, covariant=True)
TransactionT = TypeVar("TransactionT", bound=Transaction, covariant=True)


class DailyRewardPaginator:
    """A paginator specifically for claimed daily rewards"""

    client: GenshinClient
    limit: Optional[int]
    lang: Optional[str]
    current_page: Optional[int]

    page_size: int = 10

    def __init__(self, client: GenshinClient, limit: int = None, lang: str = None) -> None:
        self.client = client
        self.limit = limit
        self.lang = lang

        self.current_page = 1

    @property
    def exhausted(self) -> bool:
        return self.current_page is None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(limit={self.limit})"

    async def _get_page(self, page: int) -> List[ClaimedDailyReward]:
        params = dict(current_page=page)
        data = await self.client.request_daily_reward("award", params=params, lang=self.lang)
        return [ClaimedDailyReward(**i) for i in data["list"]]

    async def next_page(self) -> List[ClaimedDailyReward]:
        if self.current_page is None:
            raise Exception("No more pages")

        data = await self._get_page(self.current_page)

        if len(data) < self.page_size:
            self.current_page = None
            return data

        self.current_page += 1
        return data

    async def _iter(self) -> AsyncIterator[ClaimedDailyReward]:
        """Iterate over pages until the end"""
        while not self.exhausted:
            page = await self.next_page()
            for i in page:
                yield i

    def __aiter__(self) -> AsyncIterator[ClaimedDailyReward]:
        """Iterate over all pages unril the limit is reached"""
        return aislice(self._iter(), self.limit)

    async def flatten(self) -> List[ClaimedDailyReward]:
        """Flatten the entire iterator into a list"""
        # sending more than 1 request at once causes a ratelimit
        # that means no posible greedy flatten implementation
        return [item async for item in self]


class IDPagintor(Generic[IDModelT]):
    """A paginator of genshin end_id pages"""

    __repr_args__: Sequence[str] = ["limit"]

    client: GenshinClient
    limit: Optional[int]
    end_id: Optional[int]

    page_size: int = 20

    def __init__(self, client: GenshinClient, *, limit: int = None, end_id: int = 0) -> None:
        """Create a new paginator from a limit and the starting end id"""
        self.client = client
        self.limit = limit
        self.end_id = end_id

    @property
    def exhausted(self) -> bool:
        return self.end_id is None

    def __repr__(self) -> str:
        args = ", ".join(f"{i}={getattr(self, i)!r}" for i in self.__repr_args__)
        return f"{type(self).__name__}({args})"

    async def _get_page(self, end_id: int) -> List[IDModelT]:
        raise NotImplementedError

    def _cache_key(self, end_id: int) -> Tuple[int, str]:
        return (end_id, "")

    def _update_cache(self, data: List[IDModelT]) -> bool:
        if self.client.paginator_cache is None:
            return False

        cache = self.client.paginator_cache

        if self.end_id:
            cache[self._cache_key(self.end_id)] = data[0]

        for p, n in zip(data, data[1:]):
            cache[self._cache_key(p.id)] = n

        return True

    def _collect_cache(self) -> Iterator[IDModelT]:
        cache = self.client.paginator_cache
        if cache is None or self.end_id is None:
            return

        key = self._cache_key(self.end_id)
        while key in cache:
            yield cache[key]
            self.end_id = cache[key].id
            key = self._cache_key(self.end_id)

    async def next_page(self) -> List[IDModelT]:
        """Get the next page of the paginator"""
        if self.end_id is None:
            raise Exception("No more pages")

        data = await self._get_page(self.end_id)

        self._update_cache(data)

        # mark paginator as exhausted
        if len(data) < self.page_size:
            self.end_id = None
            return data

        self.end_id = data[-1].id
        return data

    async def _iter(self) -> AsyncIterator[IDModelT]:
        """Iterate over pages until the end"""
        # tfw no yield from in asyn iterators
        while self.end_id is not None:
            for i in self._collect_cache():
                yield i

            page = await self.next_page()
            for i in page:
                yield i

    def __aiter__(self) -> AsyncIterator[IDModelT]:
        """Iterate over all pages unril the limit is reached"""
        return aislice(self._iter(), self.limit)

    async def flatten(self) -> List[IDModelT]:
        """Flatten the entire iterator into a list"""
        return [item async for item in self]

    async def first(self) -> IDModelT:
        """Get the very first item"""
        x = await self._iter().__anext__()
        self.end_id = None  # invalidate the iterator
        return x


class AuthkeyPaginator(IDPagintor[IDModelT]):
    __repr_args__ = ["limit", "lang"]

    _authkey: Optional[str]
    _lang: Optional[str]

    def __init__(
        self,
        client: GenshinClient,
        lang: str = None,
        authkey: str = None,
        limit: int = None,
        end_id: int = 0,
    ) -> None:
        super().__init__(client, limit=limit, end_id=end_id)
        self._lang = lang
        self._authkey = authkey

    @property
    def lang(self) -> str:
        return self._lang or self.client.lang

    @property
    def authkey(self) -> str:
        authkey = self._authkey or self.client.authkey
        if authkey is None:
            raise RuntimeError("No authkey set for client")

        return authkey


class WishHistory(AuthkeyPaginator[Wish]):
    __repr_args__ = ["banner_type", "limit", "lang"]

    client: GenshinClient
    banner_type: int

    def __init__(self, client: GenshinClient, banner_type: int, **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        if banner_type not in [100, 200, 301, 302]:
            raise ValueError(f"Invalid banner type: {banner_type!r}")
        self.banner_type = banner_type

    def _cache_key(self, end_id: int) -> Tuple[int, str]:
        return (end_id, self.lang)

    async def _get_banner_name(self) -> str:
        """Get the banner name of banner_type"""
        banner_types = await self.client.get_banner_names(lang=self._lang, authkey=self._authkey)
        return banner_types[self.banner_type]

    async def _get_page(self, end_id: int) -> List[Wish]:
        data = await self.client.request_gacha_info(
            "getGachaLog",
            lang=self._lang,
            authkey=self._authkey,
            params=dict(gacha_type=self.banner_type, size=self.page_size, end_id=end_id),
        )
        banner_name = await self._get_banner_name()
        return [Wish(**i, banner_name=banner_name) for i in data["list"]]


class Transactions(AuthkeyPaginator[TransactionT]):
    __repr_args__ = ["kind", "limit", "lang"]

    client: GenshinClient
    kind: str

    def __init__(self, client: GenshinClient, kind: str, **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        if kind not in ["primogem", "crystal", "resin", "artifact", "weapon"]:
            raise ValueError(f"Invalid transaction kind: {kind}")
        self.kind = kind

    def _cache_key(self, end_id: int) -> Tuple[int, str]:
        return (end_id, self.lang)

    async def _get_page(self, end_id: int):
        endpoint = "get" + self.kind.capitalize() + "Log"

        data, reasons = await asyncio.gather(
            self.client.request_transaction(
                endpoint,
                lang=self._lang,
                authkey=self._authkey,
                params=dict(end_id=end_id, size=20),
            ),
            self.client._get_transaction_reasons(self.lang),
        )

        transactions = []
        for trans in data["list"]:
            cls = ItemTransaction if "name" in trans else Transaction
            reason = reasons.get(trans["reason"], "")
            transactions.append(cls(**trans, reason_str=reason, kind=self.kind))

        return transactions


class MergedPaginator(AuthkeyPaginator[IDModelT]):
    _paginators: List[IDPagintor[IDModelT]]
    _key: Callable[[IDModelT], Any]

    def __init__(self, client: GenshinClient, **kwargs: Any) -> None:
        super().__init__(client, **kwargs)

    def _iter(self) -> AsyncIterator[IDModelT]:
        return amerge(self._paginators, key=self._key)

    async def flatten(self, *, lazy: bool = False) -> List[IDModelT]:
        if self.limit is not None and lazy:
            it = aislice(amerge(self._paginators, key=self._key), self.limit)
            return [x async for x in it]

        coros = (p.flatten() for p in self._paginators)
        lists = await asyncio.gather(*coros)
        return list(heapq.merge(*lists, key=self._key))[: self.limit]


class MergedWishHistory(MergedPaginator[Wish]):
    __repr_args__ = ["banner_types", "limit", "lang"]

    client: GenshinClient
    banner_types: List[int]

    def __init__(
        self, client: GenshinClient, banner_types: List[int] = None, **kwargs: Any
    ) -> None:
        super().__init__(client, **kwargs)
        self.banner_types = banner_types or [100, 200, 301, 302]

        self._paginators = [WishHistory(client, b, **kwargs) for b in self.banner_types]
        self._key: Callable[[Wish], float] = lambda wish: -wish.time.timestamp()

    async def flatten(self, *, lazy: bool = False) -> List[Wish]:
        # before we gather all histories we should get the banner name
        asyncio.create_task(self.client.get_banner_names(lang=self._lang, authkey=self._authkey))
        return await super().flatten(lazy=lazy)


class MergedTransactions(MergedPaginator[Union[Transaction, ItemTransaction]]):
    __repr_args__ = ["kinds", "limit", "lang"]

    client: GenshinClient
    kinds: List[str]

    def __init__(self, client: GenshinClient, kinds: List[str] = None, **kwargs: Any) -> None:
        super().__init__(client, **kwargs)
        self.kinds = kinds or ["primogem", "crystal", "resin", "artifact", "weapon"]

        self._paginators = [Transactions(client, kind, **kwargs) for kind in self.kinds]
        self._key: Callable[[Transaction], float] = lambda trans: -trans.time.timestamp()