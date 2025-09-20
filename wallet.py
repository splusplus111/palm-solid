import asyncio
import base64
import json
from typing import Optional, Union

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash as SHash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.transaction import Transaction, VersionedTransaction

from config import (
    SOLANA_RPC_URL,
    WALLET_PRIVATE_KEY_JSON,
)

# Program IDs (solders Pubkey)
SYS_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def _coerce_pubkey(x: Union[str, Pubkey]) -> Pubkey:
    """Accept str (base58) or Pubkey; reject Hash/bytes to avoid subtle bugs."""
    if isinstance(x, Pubkey):
        return x
    if isinstance(x, str):
        return Pubkey.from_string(x)
    if isinstance(x, (bytes, bytearray, memoryview)):
        raise TypeError("Expected base58 string or Pubkey; got raw bytes")
    if isinstance(x, SHash):
        raise TypeError("Expected Pubkey; got solders.Hash (likely a blockhash).")
    raise TypeError(f"Unsupported pubkey-like type: {type(x)}")


def _find_ata(owner: Pubkey, mint: Pubkey, token_program_id: Pubkey = TOKEN_PROGRAM_ID) -> Pubkey:
    """Derive the Associated Token Account PDA."""
    seeds = [b"ata", bytes(owner), bytes(token_program_id), bytes(mint)]
    ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
    return ata


async def confirm_signature(client: AsyncClient, sig: str, *, commitment: str = "confirmed", timeout_s: float = 3.0) -> bool:
    """
    Poll getSignatureStatuses until the tx reaches the given commitment
    or timeout expires. Returns True if confirmed/finalized.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    params = [[sig], {"searchTransactionHistory": False}]
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await client._provider.make_request("getSignatureStatuses", params)
            st_list = (r.get("result") or {}).get("value") or [None]
            st = st_list[0]
            if st and st.get("confirmationStatus") in (commitment, "finalized"):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.15)
    return False


class Wallet:
    def __init__(self) -> None:
        raw = json.loads(WALLET_PRIVATE_KEY_JSON)
        if isinstance(raw, list):
            kp_bytes = bytes(raw)
        else:
            kp_bytes = bytes(json.loads(raw))
        self.kp = Keypair.from_bytes(kp_bytes)
        self.client = AsyncClient(SOLANA_RPC_URL)
        self._pubkey_cache = self.kp.pubkey()

    # ------------ Convenience ------------
    @property
    def pubkey(self) -> Pubkey:
        return self._pubkey_cache

    @property
    def pubkey_str(self) -> str:
        return str(self._pubkey_cache)

    async def close(self) -> None:
        await self.client.close()

    # Quick wrapper so sniper can call `await wallet.confirm(sig, ...)`
    async def confirm(self, sig: str, *, commitment: str = "confirmed", timeout_s: float = 3.0) -> bool:
        return await confirm_signature(self.client, sig, commitment=commitment, timeout_s=timeout_s)

    # ------------ RPC helpers ------------
    async def get_lamports(self) -> int:
        resp = await self.client.get_balance(self.pubkey)
        return resp.value

    async def _get_latest_blockhash(self) -> SHash:
        resp = await self.client.get_latest_blockhash()
        bh58 = resp.value.blockhash
        if isinstance(bh58, str):
            return SHash.from_string(bh58)
        if isinstance(bh58, SHash):
            return bh58
        raise TypeError(f"Unexpected blockhash type: {type(bh58)}")

    # Token balance helper
    async def get_token_balance(self, mint: Union[str, Pubkey], *, token_program_id: Optional[Pubkey] = None) -> Optional[float]:
        """
        Returns the UI amount in the ATA for (owner=self, mint), or:
          - 0.0 if ATA exists but empty
          - None if ATA does not exist (yet)
        """
        mint_pk = _coerce_pubkey(mint)
        tpid = token_program_id or TOKEN_PROGRAM_ID
        ata = _find_ata(self.pubkey, mint_pk, tpid)
        try:
            bal = await self.client.get_token_account_balance(ata)
            if bal.value is None:
                return None
            # prefer exact 0.0 when it's truly empty
            try:
                return float(bal.value.ui_amount_string)
            except Exception:
                return None
        except Exception:
            # likely account not yet created
            return None

    # ------------ ATA helpers ------------
    async def get_ata_address(self, mint: Union[str, Pubkey], *, token_program_id: Optional[Pubkey] = None) -> str:
        mint_pk = _coerce_pubkey(mint)
        tpid = token_program_id or TOKEN_PROGRAM_ID
        ata = _find_ata(self.pubkey, mint_pk, tpid)
        return str(ata)

    async def create_associated_token_account(
        self,
        mint: Union[str, Pubkey],
        *,
        token_program_id: Optional[Pubkey] = None
    ) -> str:
        mint_pk = _coerce_pubkey(mint)
        tpid = token_program_id or TOKEN_PROGRAM_ID
        ata = _find_ata(self.pubkey, mint_pk, tpid)

        try:
            info = await self.client.get_account_info(ata)
            if info.value is not None:
                return str(ata)
        except Exception:
            pass

        metas = [
            AccountMeta(pubkey=self.kp.pubkey(), is_signer=True, is_writable=True),  # payer
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),             # ATA
            AccountMeta(pubkey=self.kp.pubkey(), is_signer=False, is_writable=False), # owner
            AccountMeta(pubkey=mint_pk, is_signer=False, is_writable=False),        # mint
            AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=tpid, is_signer=False, is_writable=False),           # token program
            AccountMeta(pubkey=RENT_SYSVAR_ID, is_signer=False, is_writable=False),
        ]
        ix = Instruction(program_id=ASSOCIATED_TOKEN_PROGRAM_ID, accounts=metas, data=b"")
        bh = await self._get_latest_blockhash()
        msg = Message.new_with_blockhash([ix], self.kp.pubkey(), bh)
        unsigned_tx = Transaction.new_unsigned(msg)

        # Sign explicitly here instead of using _sign_and_serialize
        signed_tx = Transaction(unsigned_tx.message, [self.kp], bh)

        await self.client.send_raw_transaction(bytes(signed_tx), opts=TxOpts(skip_preflight=True, max_retries=3))
        return str(ata)

    async def try_close_ata(
        self,
        mint: Union[str, Pubkey],
        *,
        token_program_id: Optional[Pubkey] = None,
        preflight: bool = True
    ) -> bool:
        """
        Attempts to close an empty ATA to reclaim rent. Returns True if close tx was sent.
        """
        mint_pk = _coerce_pubkey(mint)
        tpid = token_program_id or TOKEN_PROGRAM_ID
        ata = _find_ata(self.pubkey, mint_pk, tpid)

        # Ensure it's empty (and exists)
        try:
            bal = await self.client.get_token_account_balance(ata)
            if bal.value is None:
                return False
            if float(bal.value.ui_amount_string) != 0.0:
                return False
        except Exception:
            return False

        metas = [
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),               # account (ATA)
            AccountMeta(pubkey=self.kp.pubkey(), is_signer=False, is_writable=True),  # destination for rent
            AccountMeta(pubkey=self.kp.pubkey(), is_signer=True, is_writable=False),  # authority (owner)
        ]

        # Create the close_account instruction (discriminator 9)
        ix = Instruction(
            program_id=TOKEN_PROGRAM_ID,
            accounts=metas,
            data=bytes([9])  # '9' means close_account instruction
        )

        bh = await self._get_latest_blockhash()
        msg = Message.new_with_blockhash([ix], self.kp.pubkey(), bh)
        unsigned_tx = Transaction.new_unsigned(msg)

        # sign with your keypair explicitly here
        signed_tx = Transaction(unsigned_tx.message, [self.kp], bh)

        try:
            await self.client.send_raw_transaction(bytes(signed_tx), opts=TxOpts(skip_preflight=not preflight, max_retries=3))
            return True
        except Exception:
            return False

    # ------------ Jupiter tx sender ------------
    async def send_serialized_tx(self, tx_base64: str) -> str:
        """
        Takes a base64-encoded v0 transaction from Jupiter, signs with our key, and sends it.
        Returns the transaction signature (base58).
        """
        try:
            raw = base64.b64decode(tx_base64)
        except Exception as e:
            raise ValueError(f"Failed to b64-decode Jupiter tx: {e}")

        try:
            unsigned = VersionedTransaction.from_bytes(raw)
        except Exception as e:
            raise ValueError(f"Failed to parse VersionedTransaction: {e}")

        # Sign (Jup v0 message already includes blockhash + ALTs)
        signed = VersionedTransaction(unsigned.message, [self.kp])

        resp = await self.client.send_raw_transaction(bytes(signed), opts=TxOpts(skip_preflight=True, max_retries=3))
        return resp.value

    async def _sign_and_serialize(self, transaction: Transaction) -> bytes:
        # Fetch latest blockhash
        blockhash = await self._get_latest_blockhash()

        # Create a new signed transaction with the blockhash
        transaction = Transaction(transaction.message, [self.kp], blockhash)

        return transaction.serialize()
