from eth_typing import HexStr
from hexbytes import HexBytes
from web3 import Web3
from web3.method import Method
from web3.module import Module
from web3.types import RPCEndpoint, Nonce, _Hash32
from typing import Any, List, Optional, Callable, Union
from functools import reduce
from eth_account._utils.legacy_transactions import Transaction, encode_transaction

from .types import (
    FlashbotsOpts,
    FlashbotsBundleRawTx,
    FlashbotsBundleTx,
    FlashbotsBundleDictTx,
)
import time

SECONDS_PER_BLOCK = 13.25


class FlashbotsRPC:
    eth_sendBundle = RPCEndpoint("eth_sendBundle")
    eth_callBundle = RPCEndpoint("eth_callBundle")


class FlashbotsTransactionResponse:
    w3: Web3
    bundle: List[Any]
    target_block_number: int

    def __init__(self, w3: Web3, txs: List[HexBytes], target_block_number: int):
        self.w3 = w3

        # TODO: Parse them instead
        # TODO: Add type
        def parse_tx(tx):
            return {
                "signed_transaction": tx,
                "hash": self.w3.sha3(tx),
                # todo, decode and add account/nonce
            }

        self.bundle = list(map(parse_tx, txs))
        self.target_block_number = target_block_number

    def wait(self) -> None:
        """ Waits until the target block has been reached """
        while self.w3.eth.blockNumber < self.target_block_number:
            time.sleep(1)

    def receipts(self) -> List[Union[_Hash32, HexBytes, HexStr]]:
        """ Returns all the transaction receipts from the submitted bundle """
        self.wait()
        return list(
            map(lambda tx: self.w3.eth.getTransactionReceipt(tx["hash"]), self.bundle)
        )


class Flashbots(Module):
    signed_txs: List[HexBytes]
    response: FlashbotsTransactionResponse

    def sign_bundle(
        self,
        bundled_transactions: List[
            Union[FlashbotsBundleTx, FlashbotsBundleRawTx, FlashbotsBundleDictTx]
        ],
    ) -> List[HexBytes]:
        """ Given a bundle of signed and unsigned transactions, it signs them all"""
        nonces = {}
        signed_transactions = []
        for tx in bundled_transactions:
            if "signed_transaction" in tx:
                signed_transactions.append(tx["signed_transaction"])
            elif "signer" in tx:
                # set all the fields
                signer = tx["signer"]
                tx = tx["transaction"]
                if tx["nonce"] is None:
                    nonce = nonces.get(signer.address) or Nonce(0)
                    tx["nonce"] = nonce
                else:
                    nonce = tx["nonce"]

                # store the new nonce
                nonces[signer.address] = nonce + 1

                # and update the tx details
                tx["from"] = signer.address
                tx["gasPrice"] = 0
                if "gas" not in tx:
                    tx["gas"] = self.web3.eth.estimateGas(tx)
                # sign the tx
                signed_tx = signer.sign_transaction(tx)
                signed_transactions.append(signed_tx.rawTransaction)
            elif all(key in tx for key in ["v", "r", "s"]):
                # transaction dict taken from w3.eth.get_block('pending', full_transactions=True)
                v, r, s = (
                    tx["v"],
                    int(tx["r"].hex(), base=16),
                    int(tx["s"].hex(), base=16),
                )
                raw = encode_transaction(
                    Transaction(
                        v=v,
                        r=r,
                        s=s,
                        data=HexBytes(tx["input"]),
                        gas=tx["gas"],
                        gasPrice=tx["gasPrice"],
                        nonce=tx["nonce"],
                        to=HexBytes(tx["to"]) if "to" in tx else None,
                        value=tx["value"],
                    ),
                    (v, r, s),
                )
                signed_transactions.append(raw)

        return signed_transactions

    def to_hex(self, signed_transaction: bytes) -> str:
        tx_hex = signed_transaction.hex()
        if tx_hex[0:2] != "0x":
            tx_hex = f"0x{tx_hex}"
        return tx_hex

    def send_raw_bundle_munger(
        self,
        signed_bundled_transactions: List[HexBytes],
        target_block_number: int,
        opts: Optional[FlashbotsOpts] = None,
    ) -> List[Any]:
        """ Given a raw signed bundle, it packages it up with the block numbre and the timestamps """
        # convert to hex
        return [
            {
                "txs": list(map(lambda x: self.to_hex(x), signed_bundled_transactions)),
                "blockNumber": hex(target_block_number),
                "minTimestamp": opts["minTimestamp"] if opts else 0,
                "maxTimestamp": opts["maxTimestamp"] if opts else 0,
                "revertingTxHashes": opts["revertingTxHashes"] if opts else [],
            }
        ]

    sendRawBundle: Method[Callable[[Any], Any]] = Method(
        FlashbotsRPC.eth_sendBundle, mungers=[send_raw_bundle_munger]
    )
    send_raw_bundle = sendRawBundle

    def send_bundle_munger(
        self,
        bundled_transactions: List[Union[FlashbotsBundleTx, FlashbotsBundleRawTx]],
        target_block_number: int,
        opts: Optional[FlashbotsOpts] = None,
    ) -> List[Any]:
        signed_txs = self.sign_bundle(bundled_transactions)
        self.response = FlashbotsTransactionResponse(
            self.web3, signed_txs, target_block_number
        )
        return self.send_raw_bundle_munger(signed_txs, target_block_number, opts)

    def raw_bundle_formatter(self, resp) -> Any:
        return lambda _: resp.response

    sendBundle: Method[Callable[[Any], Any]] = Method(
        FlashbotsRPC.eth_sendBundle,
        mungers=[send_bundle_munger],
        result_formatters=raw_bundle_formatter,
    )
    send_bundle = sendBundle

    def simulate(
        self,
        bundled_transactions,
        block_tag: int = None,
        state_block_tag: int = None,
        block_timestamp: int = None,
    ):
        # get block details
        block_details = (
            self.web3.eth.get_block(block_tag)
            if block_tag is not None
            else self.web3.eth.get_block("latest")
        )

        # sets evm params
        evm_block_number = self.web3.toHex(block_details.number)
        evm_block_state_number = (
            state_block_tag
            if state_block_tag is not None
            else self.web3.toHex(block_details.number - 1)
        )
        evm_timestamp = (
            block_timestamp
            if block_timestamp is not None
            else self.extrapolate_timestamp(block_tag, block_details.number)
        )

        signed_bundled_transactions = self.sign_bundle(bundled_transactions)
        # calls evm simulator
        call_result = self.call_bundle(
            signed_bundled_transactions,
            evm_block_number,
            evm_block_state_number,
            evm_timestamp,
        )

        return {
            "bundleHash": call_result["bundleHash"],
            "coinbaseDiff": call_result["coinbaseDiff"],
            "results": call_result["results"],
            "signedBundledTransactions": signed_bundled_transactions,
            "totalGasUsed": reduce(
                lambda a, b: a + b["gasUsed"], call_result["results"], 0
            ),
        }

    def extrapolate_timestamp(self, block_tag: int, latest_block_number: int):
        block_delta = block_tag - latest_block_number
        if block_delta < 0:
            raise Exception("block extrapolation negative")
        return self.web3.eth.get_block(latest_block_number)["timestamp"] + (
            block_delta * SECONDS_PER_BLOCK
        )

    def call_bundle_munger(
        self,
        signed_bundled_transactions: List[
            Union[FlashbotsBundleTx, FlashbotsBundleRawTx]
        ],
        evm_block_number,
        evm_block_state_number,
        evm_timestamp,
        opts: Optional[FlashbotsOpts] = None,
    ) -> Any:
        """ Given a raw signed bundle, it packages it up with the block number and the timestamps """
        inpt = [
            {
                "txs": list(map(lambda x: x.hex(), signed_bundled_transactions)),
                "blockNumber": evm_block_number,
                "stateBlockNumber": evm_block_state_number,
                "timestamp": evm_timestamp,
            }
        ]
        return inpt

    call_bundle: Method[Callable[[Any], Any]] = Method(
        json_rpc_method=FlashbotsRPC.eth_callBundle, mungers=[call_bundle_munger]
    )
