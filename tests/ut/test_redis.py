import asyncio
import hashlib
from unittest.mock import MagicMock, call

import aioredis
import pytest
from asynctest import CoroutineMock, patch

from aioredlock.errors import LockError
from aioredlock.redis import Instance, Redis


def callculate_sha1(text):
    sha1 = hashlib.sha1()
    sha1.update(text.encode())
    digest = sha1.hexdigest()
    return digest


EVAL_OK = b'OK'
EVAL_ERROR = aioredis.errors.ReplyError('ERROR')
CANCELLED = asyncio.CancelledError('CANCELLED')
CONNECT_ERROR = OSError('ERROR')
RANDOM_ERROR = Exception('FAULT')


class FakePool:

    SET_IF_NOT_EXIST = 'SET_IF_NOT_EXIST'

    def __init__(self):

        self.script_cache = {}

        self.evalsha = CoroutineMock(return_value=True)
        self.get = CoroutineMock(return_value=False)
        self.script_load = CoroutineMock(side_effect=self._fake_script_load)

    def __await__(self):
        yield
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass

    def __call__(self):
        return self

    def is_fake(self):
        # Only for development purposes
        return True

    async def _fake_script_load(self, script):

        digest = callculate_sha1(script)
        self.script_cache[digest] = script

        return digest.encode()


class TestInstance:

    script_names = ['SET_LOCK_SCRIPT', 'UNSET_LOCK_SCRIPT']

    def test_initialization(self):

        instance = Instance(('localhost', 6379))

        assert instance.connection == ('localhost', 6379)
        assert instance._pool is None
        assert isinstance(instance._lock, asyncio.Lock)

        # scripts
        for name in self.script_names:
            assert getattr(instance, '%s_sha1' % name.lower()) is None

    @pytest.mark.parametrize("connection, address, redis_kwargs", [
        (('localhost', 6379), ('localhost', 6379), {}),
        ({'host': 'localhost', 'port': 6379, 'db': 0, 'password': 'pass'},
            ('localhost', 6379), {'db': 0, 'password': 'pass'}),
        ("redis://host:6379/0?encoding=utf-8",
            "redis://host:6379/0?encoding=utf-8", {})
    ])
    @pytest.mark.asyncio
    async def test_connect_pool_not_created(self, connection, address, redis_kwargs):
        with patch('aioredlock.redis.Instance._create_redis_pool') as \
                create_redis_pool:
            fake_pool = FakePool()
            create_redis_pool.return_value = fake_pool
            instance = Instance(connection)

            assert instance._pool is None
            pool = await instance.connect()

            create_redis_pool.assert_called_once_with(
                address, **redis_kwargs,
                minsize=1, maxsize=100)
            assert pool is fake_pool
            assert instance._pool is fake_pool

            # scripts
            assert pool.script_load.call_count == len(self.script_names)
            for name in self.script_names:
                digest = getattr(instance, '%s_sha1' % name.lower())
                assert digest
                assert digest in pool.script_cache

    @pytest.mark.asyncio
    async def test_connect_pool_already_created(self):

        with patch('aioredlock.redis.Instance._create_redis_pool') as \
                create_redis_pool:
            instance = Instance(('localhost', 6379))
            fake_pool = FakePool()
            instance._pool = fake_pool

            pool = await instance.connect()

            assert not create_redis_pool.called
            assert pool is fake_pool

            # scripts
            assert pool.script_load.call_count == 0

    @pytest.fixture
    def fake_instance(self):
        with patch('aioredlock.redis.Instance._create_redis_pool') as \
                create_redis_pool:
            fake_pool = FakePool()
            create_redis_pool.return_value = fake_pool
            instance = Instance(('localhost', 6379))
            yield instance

    @pytest.mark.asyncio
    async def test_lock(self, fake_instance):
        instance = fake_instance
        await instance.connect()
        pool = instance._pool

        await instance.set_lock('resource', 'lock_id', 10.0)

        pool.evalsha.assert_called_once_with(
            instance.set_lock_script_sha1,
            keys=['resource'],
            args=['lock_id', 10000]
        )

    @pytest.mark.asyncio
    async def test_unset_lock(self, fake_instance):
        instance = fake_instance
        await instance.connect()
        pool = instance._pool

        await instance.unset_lock('resource', 'lock_id')

        pool.evalsha.assert_called_once_with(
            instance.unset_lock_script_sha1,
            keys=['resource'],
            args=['lock_id']
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("get_return_value,locked", [
        (b'lock_identifier', True),
        (None, False),
    ])
    async def test_is_locked(self, fake_instance, get_return_value, locked):
        instance = fake_instance
        await instance.connect()
        pool = instance._pool

        pool.get.return_value = get_return_value

        res = await instance.is_locked('resource')

        assert res == locked
        pool.get.assert_called_once_with('resource')


@pytest.fixture
def redis_two_connections():
    return [
        {'host': 'localhost', 'port': 6379},
        {'host': '127.0.0.1', 'port': 6378}
    ]


@pytest.fixture
def redis_three_connections():
    return [
        {'host': 'localhost', 'port': 6379},
        {'host': '127.0.0.1', 'port': 6378},
        {'host': '8.8.8.8', 'port': 6377}
    ]


@pytest.fixture
def mock_redis_two_instances(redis_two_connections):
    pool = FakePool()
    redis = Redis(redis_two_connections, 10)

    for instance in redis.instances:
        instance._pool = pool

    yield redis, pool


@pytest.fixture
def mock_redis_three_instances(redis_three_connections):
    pool = FakePool()
    redis = Redis(redis_three_connections, 10)

    for instance in redis.instances:
        instance._pool = pool

    yield redis, pool


class TestRedis:

    def test_initialization(self, redis_two_connections):
        with patch("aioredlock.redis.Instance.__init__") as mock_instance:
            mock_instance.return_value = None

            redis = Redis(redis_two_connections, 0.01)

            calls = [
                call({'host': 'localhost', 'port': 6379}),
                call({'host': '127.0.0.1', 'port': 6378})
            ]
            mock_instance.assert_has_calls(calls)
            assert len(redis.instances) == 2
            assert redis.lock_timeout == 0.01

    parametrize_methods = pytest.mark.parametrize("method_name, call_args", [
        ('set_lock', {'keys': ['resource'], 'args':['lock_id', 10000]}),
        ('unset_lock', {'keys': ['resource'], 'args':['lock_id']}),
    ])

    @pytest.mark.asyncio
    @parametrize_methods
    async def test_lock(
            self, mock_redis_two_instances,
            method_name, call_args
    ):
        redis, pool = mock_redis_two_instances

        method = getattr(redis, method_name)
        script_sha1 = getattr(redis.instances[0],
                              '%s_script_sha1' % method_name)

        await method('resource', 'lock_id')

        calls = [call(script_sha1, **call_args)] * 2
        pool.evalsha.assert_has_calls(calls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("get_return_value,locked", [
        (b'lock_identifier', True),
        (None, False),
    ])
    async def test_is_locked(self, mock_redis_two_instances, get_return_value, locked):
        redis, pool = mock_redis_two_instances

        pool.get.return_value = get_return_value

        res = await redis.is_locked('resource')

        calls = [call('resource')] * 2
        pool.get.assert_has_calls(calls)
        assert res == locked

    @pytest.mark.asyncio
    @parametrize_methods
    async def test_lock_one_of_two_instances_failed(
            self, mock_redis_two_instances,
            method_name, call_args
    ):
        redis, pool = mock_redis_two_instances
        pool.evalsha = CoroutineMock(side_effect=[EVAL_ERROR, EVAL_OK])

        method = getattr(redis, method_name)
        script_sha1 = getattr(redis.instances[0],
                              '%s_script_sha1' % method_name)

        with pytest.raises(LockError):
            await method('resource', 'lock_id')

        calls = [call(script_sha1, **call_args)] * 2
        pool.evalsha.assert_has_calls(calls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redis_result, success", [
        ([EVAL_OK, EVAL_OK, EVAL_OK], True),
        ([EVAL_OK, EVAL_OK, EVAL_ERROR], True),
        ([EVAL_OK, EVAL_ERROR, CONNECT_ERROR], False),
        ([EVAL_ERROR, EVAL_ERROR, CONNECT_ERROR], False),
        ([EVAL_ERROR, CONNECT_ERROR, RANDOM_ERROR], False),
        ([CANCELLED, CANCELLED, CANCELLED], False),
    ])
    @parametrize_methods
    async def test_three_instances_combination(
            self,
            mock_redis_three_instances,
            redis_result,
            success,
            method_name, call_args
    ):
        redis, pool = mock_redis_three_instances
        pool.evalsha = CoroutineMock(side_effect=redis_result)

        method = getattr(redis, method_name)
        script_sha1 = getattr(redis.instances[0],
                              '%s_script_sha1' % method_name)

        if success:
            await method('resource', 'lock_id')
        else:
            with pytest.raises(LockError):
                await method('resource', 'lock_id')

        calls = [call(script_sha1, **call_args)] * 3
        pool.evalsha.assert_has_calls(calls)

    @pytest.mark.asyncio
    async def test_clear_connections(self, mock_redis_two_instances):
        redis, pool = mock_redis_two_instances
        pool.close = MagicMock()
        pool.wait_closed = CoroutineMock()

        await redis.clear_connections()

        pool.close.assert_has_calls([call(), call()])
        pool.wait_closed.assert_has_calls([call(), call()])
