import asyncio
import random
import time
import json

import numpy
from absl import logging as GLOG
from aioprocessing import AioProcess, AioQueue
from multiprocessing import Queue as MultiProcessingQueue

from typing import Callable, Union, Awaitable, Dict

from ethereum.pow.ethpow import EthashMiner
from quarkchain.env import DEFAULT_ENV
from quarkchain.config import NetworkId, ConsensusType
from quarkchain.core import MinorBlock, RootBlock
from quarkchain.utils import check
from quarkchain.utils import time_ms


class Miner:
    def __init__(
        self,
        consensus_type: ConsensusType,
        create_block_async_func: Callable[[], Awaitable[Union[MinorBlock, RootBlock]]],
        add_block_async_func: Callable[[Union[MinorBlock, RootBlock]], Awaitable[None]],
        get_target_block_time_func: Callable[[], float],
        # TODO: clean this up if confirmed not used
        env,
    ):
        """Mining will happen on a subprocess managed by this class

        create_block_async_func: takes no argument, returns a block (either RootBlock or MinorBlock)
        add_block_async_func: takes a block, add it to chain
        get_target_block_time_func: takes no argument, returns the target block time in second
        """
        check(consensus_type == ConsensusType.POW_SIMULATE)
        if consensus_type == ConsensusType.POW_SIMULATE:
            self.mine_func = Miner.simulate_mine
            self.mining_param_func = lambda: {
                "target_block_time": self.get_target_block_time_func()
            }
        elif consensus_type == ConsensusType.POW_ETHASH:
            self.mine_func = Miner.mine_ethash
            self.mining_param_func = lambda: {}
        elif consensus_type == ConsensusType.POW_SHA3SHA3:
            self.mine_func = Miner.mine_sha3sha3
            self.mining_param_func = lambda: {}
        else:
            raise ValueError("Consensus? ( う-´)づ︻╦̵̵̿╤──   \(˚☐˚”)/")

        self.create_block_async_func = create_block_async_func
        self.add_block_async_func = add_block_async_func
        self.get_target_block_time_func = get_target_block_time_func
        self.enabled = False
        self.process = None
        self.env = env

        self.input_q = AioQueue()  # [(block, target_time)]
        self.output_q = AioQueue()  # [block]

    def is_enabled(self):
        return self.enabled

    def enable(self):
        self.enabled = True

    def disable(self):
        """Stop the mining process if there is one"""
        self.enabled = False

    def mine_new_block_async(self):
        async def handle_mined_block(instance: Miner):
            while True:
                block = await instance.output_q.coro_get()
                if not block:
                    return
                try:
                    await instance.add_block_async_func(block)
                except Exception as ex:
                    GLOG.exception(ex)
                    instance.mine_new_block_async()

        async def mine_new_block(instance: Miner):
            """Get a new block and start mining.
            If a mining process has already been started, update the process to mine the new block.
            """
            block = await instance.create_block_async_func()
            mining_params = instance.mining_param_func()
            if instance.process:
                instance.input_q.put((block, mining_params))
                return

            instance.process = AioProcess(
                target=instance.mine_func,
                args=(block, instance.input_q, instance.output_q, mining_params),
            )
            instance.process.start()
            await handle_mined_block(instance)

        if not self.enabled:
            return
        return asyncio.ensure_future(mine_new_block(self))

    @staticmethod
    def __log_status(block):
        is_root = isinstance(block, RootBlock)
        shard = "R" if is_root else block.header.branch.get_shard_id()
        count = len(block.minor_block_header_list) if is_root else len(block.tx_list)
        elapsed = time.time() - block.header.create_time
        GLOG.log_every_n_seconds(
            GLOG.INFO,
            "[{}] {} [{}] ({:.2f}) {}".format(
                shard,
                block.header.height,
                count,
                elapsed,
                block.header.get_hash().hex(),
            ),
            60,
        )

    @staticmethod
    def __check_metric(metric):
        # Testnet does not check difficulty
        if DEFAULT_ENV.config.NETWORK_ID != NetworkId.MAINNET:
            return True
        return metric < 2 ** 256

    @staticmethod
    def __get_block_time(block, target_block_time) -> float:
        if isinstance(block, MinorBlock):
            # Adjust the target block time to compensate computation time
            gas_used_ratio = block.meta.evm_gas_used / block.header.evm_gas_limit
            target_block_time = target_block_time * (1 - gas_used_ratio * 0.4)
            GLOG.debug(
                "[{}] target block time {:.2f}".format(
                    block.header.branch.get_shard_id(), target_block_time
                )
            )

        return numpy.random.exponential(target_block_time)

    @staticmethod
    def simulate_mine(
        block,
        input_q: MultiProcessingQueue,
        output_q: MultiProcessingQueue,
        mining_params: Dict,
    ):
        """Sleep until the target time, or a new block is added to queue"""
        target_time = block.header.create_time + numpy.random.exponential(
            mining_params["target_block_time"]
        )
        while True:
            time.sleep(0.1)
            try:
                # raises if queue is empty
                block, mining_params = input_q.get_nowait()
                if not block:
                    output_q.put(None)
                    return
                target_time = block.header.create_time + Miner.__get_block_time(
                    block, mining_params["target_block_time"]
                )
            except Exception:
                # got nothing from queue
                pass
            if time.time() > target_time:
                Miner.__log_status(block)
                block.header.nonce = random.randint(0, 2 ** 32 - 1)
                Miner._post_process_mined_block(block)
                output_q.put(block)
                block, mining_params = input_q.get(block=True)  # blocking
                if not block:
                    output_q.put(None)
                    return
                target_time = block.header.create_time + Miner.__get_block_time(
                    block, mining_params["target_block_time"]
                )

    @staticmethod
    def mine_ethash(
        block: Union[MinorBlock, RootBlock],
        input_q: MultiProcessingQueue,
        output_q: MultiProcessingQueue,
        mining_params: Dict,
    ):
        # TODO: make it configurable, or adjustable through `mining_params`
        ROUNDS = 100
        # outer loop for mining forever
        while True:
            # `None` block means termination
            if not block:
                output_q.put(None)
                return

            header_hash = block.header.get_hash()
            block_number = block.header.height
            difficulty = block.header.difficulty
            miner = EthashMiner(block_number, difficulty, header_hash)
            start_nonce = 0
            # inner loop for iterating nonce
            while True:
                nonce_found, mixhash = miner.mine(ROUNDS, start_nonce)
                # best case
                if nonce_found:
                    block.header.nonce = int.from_bytes(nonce_found, byteorder="big")
                    Miner._post_process_mined_block(block)
                    output_q.put(block)
                    block, mining_params = input_q.get(block=True)  # blocking
                    break  # break inner loop to refresh mining params
                # check if new block arrives. if yes, discard current progress and restart
                try:
                    block, mining_params = input_q.get_nowait()
                    break  # break inner loop to refresh mining params
                except Exception:  # queue empty
                    pass
                # update param and keep mining
                start_nonce += ROUNDS

    @staticmethod
    def mine_sha3sha3(
        block,
        input_q: MultiProcessingQueue,
        output_q: MultiProcessingQueue,
        mining_params: Dict,
    ):
        pass

    @staticmethod
    def _post_process_mined_block(block: Union[MinorBlock, RootBlock]):
        if isinstance(block, RootBlock):
            extra_data = json.loads(block.header.extra_data.decode("utf-8"))
            extra_data["mined"] = time_ms()
            # NOTE this actually ruins POW mining; added for perf tracking
            block.header.extra_data = json.dumps(extra_data).encode("utf-8")
        else:
            extra_data = json.loads(block.meta.extra_data.decode("utf-8"))
            extra_data["mined"] = time_ms()
            # NOTE this actually ruins POW mining; added for perf tracking
            block.meta.extra_data = json.dumps(extra_data).encode("utf-8")
            block.header.hash_meta = block.meta.get_hash()
