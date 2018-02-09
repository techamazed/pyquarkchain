#!/usr/bin/python3

from quarkchain.genesis import create_genesis_blocks
from quarkchain.core import calculate_merkle_root, RootBlock, TransactionInput, Transaction, Code
from quarkchain.core import MinorBlock


class UtxoValue:

    def __init__(self, recipient, quarkash, rootBlockHeader):
        self.recipient = recipient
        self.quarkash = quarkash
        # Root block that requires to confirm the UTXO
        self.rootBlockHeader = rootBlockHeader


class ShardState:
    """  State of a shard, which includes
    - UTXO pool
    - minor blockchain
    - root blockchain and cross-shard transaction
    And we can perform state change either by append new block or roll back a block
    TODO: Support
    - cross-shard transaction
    - reshard by split
    """

    def __init__(self, env, genesisBlock, rootChain):
        self.env = env
        self.db = env.db
        self.genesisBlock = genesisBlock
        self.utxoPool = dict()
        self.chain = [genesisBlock]
        genesisRootBlock = rootChain.getGenesisBlock()
        # TODO: Check shard id or disable genesisBlock
        self.utxoPool[TransactionInput(genesisBlock.txList[0].getHash(), 0)] = UtxoValue(
            genesisBlock.txList[0].outList[0].address.recipient,
            genesisBlock.txList[0].outList[0].quarkash,
            genesisRootBlock.header)
        self.db.putTx(genesisBlock.txList[0], rootBlockHeader=genesisRootBlock)
        # Don't need to put txRootBlockHeader because a genesis block will
        # never be rolled back

        self.branch = self.genesisBlock.header.branch
        self.rootChain = rootChain

    def __performTx(self, tx, rootBlockHeader):
        """ Perform a transacton atomically.
        Return -1 if the transaction is invalid or
               >= 0 for the transaction fee if the transaction successfully executed.
        """

        if len(tx.inList) == 0:
            return -1

        # Make sure all tx ids from inputs:
        # - are unique; and
        # - exist in utxo pool; and
        # - depend before rootBlockHeader (inclusive)
        txInputSet = set()
        txInputQuarkash = 0
        senderList = []
        for txInput in tx.inList:
            if txInput in txInputSet:
                return -1
            if txInput not in self.utxoPool:
                return -1
            if self.utxoPool[txInput].rootBlockHeader.height > rootBlockHeader.height:
                return -1
            txInputSet.add(txInput)
            txInputQuarkash = self.utxoPool[txInput].quarkash
            senderList.append(self.utxoPool[txInput].recipient)

        # Check signature
        if not tx.verifySignature(senderList):
            return -1

        # Check if the sum of output is smaller than or equal to the input
        txOutputQuarkash = 0
        for txOut in tx.outList:
            txOutputQuarkash += txOut.quarkash
        if txOutputQuarkash > txInputQuarkash:
            return -1

        for txInput in tx.inList:
            del self.utxoPool[txInput]

        txHash = tx.getHash()
        for idx, txOutput in enumerate(tx.outList):
            if not self.branch.isInShard(txOutput.address.fullShardId):
                continue
            self.utxoPool[TransactionInput(txHash, idx)] = UtxoValue(
                txOutput.address.recipient,
                txOutput.quarkash,
                rootBlockHeader)

        self.db.putTx(tx, rootBlockHeader=rootBlockHeader, txHash=txHash)
        return txInputQuarkash - txOutputQuarkash

    def __rollBackTx(self, tx):
        txHash = tx.getHash()
        for i in range(len(tx.outList)):
            del self.utxoPool[TransactionInput(txHash, i)]

        for txInput in tx.inList:
            prevTx = self.db.getTx(txInput.hash)
            rootBlockHeader = self.db.getTxRootBlockHeader(txInput.hash)
            self.utxoPool[txInput] = UtxoValue(
                prevTx.outList[txInput.index].address.recipient,
                prevTx.outList[txInput.index].quarkash,
                rootBlockHeader)
        return None

    def appendBlock(self, block):
        """  Append a block.  This would perform validation check with local
        UTXO pool and perform state change atomically
        Return None upon success, otherwise return a string with error message
        """

        # TODO: May check if the block is already in db (and thus already
        # validated)

        if block.header.hashPrevMinorBlock != self.chain[-1].header.getHash():
            return "prev hash mismatch"

        if block.header.height != self.chain[-1].header.height + 1:
            return "height mismatch"

        # Check difficulty
        if not self.env.config.SKIP_MINOR_DIFFICULTY_CHECK:
            # TODO: Implement difficulty
            return "incorrect difficulty"

        # Make sure merkle tree is valid
        merkleHash = calculate_merkle_root(block.txList)
        if merkleHash != block.header.hashMerkleRoot:
            return "incorrect merkle root"

        # Check the first transaction of the block
        if len(block.txList) == 0:
            return "coinbase tx must exist"

        if len(block.txList[0].inList) != 0:
            return "coinbase tx's input must be empty"

        # TODO: Support multiple outputs in the coinbase tx
        if len(block.txList[0].outList) != 1:
            return "coinbase tx's output must be one"

        if not self.branch.isInShard(block.txList[0].outList[0].address.fullShardId):
            return "coinbase output must be in local shard"

        if block.txList[0].code != Code.createMinorBlockCoinbaseCode(block.header.height):
            return "incorrect coinbase code"

        # Check coinbase
        if not self.env.config.SKIP_MINOR_COINBASE_CHECK:
            # TODO: Check coinbase
            return "incorrect coinbase value"

        # Check whether the root header is in the root chain
        rootBlockHeader = self.rootChain.getBlockHeaderByHash(
            block.header.hashPrevRootBlock)
        if rootBlockHeader is None:
            return "cannot find root block for the minor block"

        txDoneList = []
        totalFee = 0
        for tx in block.txList[1:]:
            fee = self.__performTx(tx, rootBlockHeader)
            if fee < 0:
                for rTx in reversed(txDoneList):
                    rollBackResult = self.__rollBackTx(rTx)
                    assert(rollBackResult)
                return "one transaction is invalid"
            totalFee += fee

        txHash = block.txList[0].getHash()
        for idx, txOutput in enumerate(block.txList[0].outList):
            self.utxoPool[TransactionInput(txHash, idx)] = UtxoValue(
                txOutput.address.recipient,
                txOutput.quarkash,
                rootBlockHeader)

        self.db.putTx(block.txList[0], rootBlockHeader)
        self.db.put(b'mblock_' + block.header.getHash(), block.serialize())
        self.db.put(b'mblockCoinbaseTx_' + block.header.getHash(),
                    block.txList[0].serialize())
        self.chain.append(block)
        return None

    def printUtxoPool(self):
        for k, v in self.utxoPool.items():
            print("%s, %s, %s" % (k.hash.hex(), k.index, v.quarkash))

    def rollBackTip(self):
        if len(self.chain) == 1:
            return "Cannot roll back genesis block"

        block = self.chain[-1]
        del self.chain[-1]
        for rTx in reversed(block.txList[1:]):
            rollBackResult = self.__rollBackTx(rTx)
            assert(rollBackResult is None)

        txHash = block.txList[0].getHash()
        for idx in range(len(block.txList[0].outList)):
            del self.utxoPool[TransactionInput(txHash, idx)]

        return None

        # Don't need to remove db data

    def tip(self):
        """ Return the header of the tail of the shard
        """
        return self.chain[-1]

    def addCrossShardUtxo(self, txInput, utxoValue):
        assert(txInput not in self.utxoPool)
        self.utxoPool[txInput] = utxoValue

    def removeCrossShardUtxo(self, txInput):
        del self.utxoPool[txInput]

    def getBlockHeaderByHeight(self, height):
        return self.chain[height]

    def getGenesisBlock(self):
        return self.genesisBlock

    def checkBalance(self, recipient):
        balance = 0
        for k, v in self.utxoPool.items():
            if v.recipient != recipient:
                continue

            balance += v.quarkash
        return balance


class MinorChainManager:

    def __init__(self, env):
        self.env = env
        self.db = env.db
        self.rootChain = None
        self.blockPool = dict()  # hash to block header

        tmp, self.genesisBlockList = create_genesis_blocks(env)

        for mBlock in self.genesisBlockList:
            mHash = mBlock.header.getHash()
            self.db.put(b'mblock_' + mHash, mBlock.serialize())
            self.blockPool[mHash] = mBlock.header

    def setRootChain(self, rootChain):
        assert(self.rootChain is None)
        self.rootChain = rootChain

    def checkValidationByHash(self, h):
        return h in self.blockPool

    def getBlockHeader(self, h):
        return self.blockPool.get(h)

    def getBlock(self, h):
        data = self.db.get(h)
        if data is None:
            return None
        return MinorBlock.deserialize(data)

    def getGenesisBlock(self, shardId):
        return self.genesisBlockList[shardId]

    def addNewBlock(self, block):
        # TODO: validate the block
        blockHash = block.header.getHash()
        self.blockPool[blockHash] = block.header
        self.db.put(b'mblock_' + blockHash, block.serialize())
        self.db.put(b'mblockCoinbaseTx_' + blockHash,
                    block.txList[0].serialize())
        return None

    def getBlockCoinbaseTx(self, blockHash):
        return Transaction.deserialize(self.db.get(b'mblockCoinbaseTx_' + blockHash))

    def getBlockCoinbaseQuarkash(self, blockHash):
        return self.getBlockCoinbaseTx(blockHash).outList[0].quarkash


class RootChain:

    def __init__(self, env, genesisBlock=None):
        self.env = env
        self.db = env.db
        self.blockPool = dict()

        # Create genesis block if not exist
        block = genesisBlock
        if block is None:
            block, tmp = create_genesis_blocks(env)

        h = block.header.getHash()
        if b'rblock_' + h not in self.db:
            self.db.put(b'rblock_' + h, block.serialize())
        self.blockPool[h] = block.header
        self.tip = block
        self.genesisBlock = block
        self.chain = [block.header]

    def loadFromDb(self):
        # TODO
        pass

    def tip(self):
        return self.chain[-1]

    def getGenesisBlock(self):
        return self.genesisBlock

    def containBlockByHash(self, h):
        return h in self.blockPool

    def getBlockHeaderByHash(self, h):
        return self.blockPool.get(h, None)

    def getBlockHeaderByHeight(self, height):
        return self.chain[height]

    def rollBack(self):
        if len(self.chain) == 1:
            return "cannot roll back genesis block"
        del self.blockPool[self.chain[-1].getHash()]
        del self.chain[-1]
        return None

    def __checkCoinbaseTx(self, tx, height):
        if len(tx.inList) != 0:
            return False

        if tx.code != Code.createRootBlockCoinbaseCode(height):
            return False

        # We only support one output for coinbase tx
        if len(tx.outList) != 1:
            return False

        return True

    def __getBlockCoinbaseTx(self, blockHash):
        return Transaction.deserialize(self.db.get(b'mblockCoinbaseTx_' + blockHash))

    def __getBlockCoinbaseQuarkash(self, blockHash):
        return self.__getBlockCoinbaseTx(blockHash).outList[0].quarkash

    def appendBlock(self, block, uncommittedMinorBlockHeaderSet):
        """ Append new block.
        There are a couple of optimizations can be done here:
        - the root block could only contain minor block header hashes as long as the shards fully validate the headers
        - the header (or hashes) are un-ordered as long as they contains valid sub-chains from previous root block
        """

        if block.header.hashPrevBlock != self.chain[-1].getHash():
            return "previous hash block mismatch"

        if block.header.height != len(self.chain):
            return "height mismatch"

        if block.header.hashCoinbaseTx != block.coinbaseTx.getHash():
            return "coinbase tx hash mismatch"

        if not self.__checkCoinbaseTx(block.coinbaseTx, block.header.height):
            return "incorrect coinbase tx"

        blockHash = block.header.getHash()
        prevBlock = RootBlock.deserialize(self.db.get(
            b'rblock_' + block.header.hashPrevBlock))

        # Check the merkle tree
        merkleHash = calculate_merkle_root(block.minorBlockHeaderList)
        if merkleHash != block.header.hashMerkleRoot:
            return "incorrect merkle root"

        # Check difficulty
        if not self.env.config.SKIP_ROOT_DIFFICULTY_CHECK:
            # TOOD: Implement difficulty
            return "insufficient difficulty"

        # Check whether all minor blocks are validated
        for mheader in block.minorBlockHeaderList:
            if mheader not in uncommittedMinorBlockHeaderSet:
                return "root block confirms non-exist block"
            # Check shard size matches
            if mheader.branch.getShardSize() != block.header.shardInfo.getShardSize():
                return "root block shard size mismatches minor header"

        # Check whether all minor blocks are ordered (and linked to previous block)
        # Find the last block of previous block
        shardId = 0
        lastBlockHashList = []
        prevHeader = prevBlock.minorBlockHeaderList[0]
        for mheader in prevBlock.minorBlockHeaderList:
            if shardId != mheader.branch.getShardId():
                assert(shardId + 1 == mheader.branch.getShardId())
                lastBlockHashList.append(prevHeader.getHash())
            prevHeader = mheader
        lastBlockHashList.append(prevBlock.minorBlockHeaderList[-1].getHash())
        assert(len(lastBlockHashList) ==
               prevBlock.header.shardInfo.getShardSize())

        shardId = 0
        prevHeader = block.minorBlockHeaderList[0]
        blockCountInShard = 1
        if prevHeader.branch.getShardId() != 0:
            return "first minor block header must start from shard 0"
        if prevHeader.hashPrevMinorBlock != lastBlockHashList[0]:
            return "first minor block in shard doesn't link to previous one in previous root block"

        totalMinorCoinbase = self.__getBlockCoinbaseQuarkash(
            block.minorBlockHeaderList[0].getHash())
        for mheader in block.minorBlockHeaderList[1:]:
            totalMinorCoinbase += self.__getBlockCoinbaseQuarkash(
                mheader.getHash())
            if mheader.branch.getShardId() == shardId:
                # Check if all minor blocks are linked in the shard
                if mheader.hashPrevMinorBlock != prevHeader.getHash():
                    return "minor block doesn't link to previous minor block"
                blockCountInShard += 1
            elif mheader.branch.getShardId() != shardId + 1:
                # Shard id is unordered
                return "shard id must be ordered"
            else:
                if blockCountInShard < self.env.config.PROOF_OF_PROGRESS_BLOCKS:
                    return "fail to prove progress"
                # New shard is found in the list
                shardId = mheader.branch.getShardId()
                if mheader.hashPrevMinorBlock != lastBlockHashList[shardId]:
                    return "first minor block in shard doesn't link to previous one in previous root block"

            prevHeader = mheader
        if shardId != block.header.shardInfo.getShardSize() - 1:
            return "fail to prove progress"
        if blockCountInShard < self.env.config.PROOF_OF_PROGRESS_BLOCKS:
            return "fail to prove progress"

        # Check the coinbase value is valid (we allow burning coins)
        if block.coinbaseTx.outList[0].quarkash > totalMinorCoinbase:
            return "incorrect coinbase quarkash"

        # Add the block hash to block header to memory pool and add the block
        # to db
        self.blockPool[blockHash] = block.header
        self.chain.append(block.header)
        self.db.put(b"rblock_" + blockHash, block.serialize())

        # Remove all uncommit blocks
        for mheader in block.minorBlockHeaderList:
            uncommittedMinorBlockHeaderSet.remove(mheader)

        return None


class QuarkChain:

    def __init__(self, env):
        self.minorChainManager = MinorChainManager(env)
        self.rootChain = RootChain(env)
        self.minorChainManager.setRootChain(self.rootChain)


class QuarkChainState:
    """ TODO: Support reshard
    """

    def __init__(self, env):
        self.env = env
        self.db = env.db
        rBlock, mBlockList = create_genesis_blocks(env)
        self.rootChain = RootChain(env, rBlock)
        self.shardList = [ShardState(env, mBlock, self.rootChain)
                          for mBlock in mBlockList]
        self.blockToCrossShardUtxoMap = dict()
        self.uncommittedMinorBlockHeaderSet = set()

    def __addCrossShardTxFrom(self, mBlock, rBlock):
        shardSize = len(self.shardList)
        for tx in mBlock.txList[1:]:
            txHash = tx.getHash()
            for idx, txOutput in enumerate(tx.outList):
                shardId = txOutput.address.fullShardId & (shardSize - 1)
                if shardId == mBlock.header.branch.getShardId():
                    continue
                self.shardList[shardId].addCrossShardUtxo(
                    TransactionInput(txHash, idx),
                    UtxoValue(
                        txOutput.address.recipient,
                        txOutput.quarkash,
                        rBlock.header))

    def __removeCrossShardTxFrom(self, mBlock):
        shardSize = len(self.shardList)
        for tx in mBlock.txList[1:]:
            txHash = tx.getHash()
            for idx, txOutput in enumerate(tx.outList):
                shardId = txOutput.address.fullShardId & (shardSize - 1)
                if shardId == mBlock.header.branch.getShardId():
                    continue
                self.shardList[shardId].removeCrossShardUtxo(
                    TransactionInput(txHash, idx))

    def appendMinorBlock(self, mBlock):
        if mBlock.header.branch.getShardSize() != len(self.shardList):
            return "minor block shard size is too large"

        appendResult = self.shardList[
            mBlock.header.branch.getShardId()].appendBlock(mBlock)
        if appendResult is not None:
            return appendResult

        self.uncommittedMinorBlockHeaderSet.add(mBlock.header)
        return None

    def rollBackMinorBlock(self, shardId):
        """ Roll back a minor block of a shard.
        The minor block must not be commited by root blocks.
        """
        if shardId > len(self.shardList):
            return "shard id is too large"
        shard = self.shardList[shardId]
        if shard.tip not in self.uncommittedMinorBlockHeaderSet:
            """ Root block already commits the minor blocks.
            Need to roll back root block before rolling back the minor block.
            """
            return "the minor block is commited by root block"
        return shard.rollBackTip()

    def appendRootBlock(self, rBlock):
        """ Append a root block to rootChain
        """
        appendResult = self.rootChain.appendBlock(
            rBlock, self.uncommittedMinorBlockHeaderSet)
        if appendResult is not None:
            return appendResult

        for mHeader in rBlock.minorBlockHeaderList:
            mBlock = self.db.getMinorBlockByHash(mHeader.getHash())
            self.__addCrossShardTxFrom(mBlock, rBlock)

        return None

        # TODO: Add root block coinbase tx

    def rollBackRootBlock(self):
        """ Roll back a root block in rootChain
        """
        rBlockHeader = self.rootChain.tip()
        rBlockHash = rBlockHeader.gethHash()
        rBlock = self.db.getRootBlockByHash(rBlockHash)
        for mHeader in self.uncommittedMinorBlockHeaderSet:
            if mHeader.hashPrevRootBlock == rBlockHash:
                # Cannot roll back the root block since it is being used.
                return "the root block is used by uncommitted minor blocks"

        result = self.rootChain.rollBack()
        if result is not None:
            return result

        for mHeader in rBlock.minorBlockHeaderList:
            self.uncommittedMinorBlockHeaderSet.add(mHeader)

        return None

        # TODO: Remove root block coinbase tx

    def getMinorBlockHeaderByHeight(self, shardId, height):
        return self.shardList[shardId].getBlockHeaderByHeight(height)

    def getRootBlockHeaderByHeight(self, height):
        return self.rootChain.getBlockHeaderByHeight(height)

    def getGenesisMinorBlock(self, shardId):
        return self.shardList[shardId].getGenesisBlock()

    def getGenesisRootBlock(self):
        return self.rootChain.getGenesisBlock()