"""Durable, account-scoped digital delivery and order-price workflow.

No network or application-global imports: tests can exercise the real state machine.
SQLite transactions never span an await. Uncertain sends are never resent blindly.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re
import secrets
import sqlite3
import time
import threading

_PAID_MARKERS = {'TRADE_PAID_DONE_SELLER', 'REMIND_DELIVER_SELLER'}
_PAID_REMINDERS = {'等待卖家发货'}
_PAID_BUTTONS = {'已付款', '去发货'}
_PAID_CONTENTS = {'[我已付款，等待你发货]', '[已付款，待发货]', '[记得及时发货]'}


def money(value):
    try:
        amount = Decimal(str(value).replace('¥', '').replace('￥', '').replace(',', '').strip())
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('金额必须为非负数，且最多两位小数')
        return amount
    except (InvalidOperation, TypeError):
        raise ValueError('金额格式无效')


_PRICE_NUMBER = r'(?<![\d.])\d+(?:\.\d{1,2})?(?![\d.])'


def quoted_seller_price(text, target):
    """Prefer explicit offers over rejected prices and the original list price.

    For example: '2.5做不了，原价3块，给您¥2.7' is an offer of 2.7,
    not three competing offers. Ambiguous multi-price text still fails closed.
    """
    target_mentions = [m for m in re.finditer(_PRICE_NUMBER, text) if money(m.group()) == target]
    if not target_mentions:
        return False
    for mention in target_mentions:
        before = text[max(0, mention.start() - 12):mention.start()]
        after = text[mention.end():mention.end() + 12]
        if (re.search(r'(?:不接受|不能|不卖|不出|不考虑|做不了|不可以|不支持|不同意)[^，,。！？!?\d]{0,6}[¥￥]?\s*$', before)
                or re.match(r'\s*(?:元|块钱|块)?\s*(?:真的|确实|也|还是)?\s*(?:不行|不能|不卖|不出|做不了|不可以|不接受)', after)):
            return False
    symbols = re.findall(r'[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])', text)
    if symbols and {money(p) for p in symbols} == {target}:
        return True
    offers = re.findall(r'(?:改成|改为|改到|成交价|优惠价|最低|底价|只要|算你|算您|给你|给您)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)(?![\d.])', text)
    offers += re.findall(r'(?<![\d.])(\d+(?:\.\d{1,2})?)(?![\d.])\s*(?:元|块钱|块)?\s*(?:可以|成交|拿走|给你|给您)', text)
    if offers:
        return {money(p) for p in offers} == {target}
    amounts = re.findall(_PRICE_NUMBER, text)
    return {money(p) for p in amounts} == {target} and not re.search(r'不行|不能|不卖|拒绝|做不了|不接受|不支持|不同意', text)


def _json_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value[:1] == '{':
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _trade_envelope(message):
    """Locate platform trade metadata in a full card, map-update, or skinny reminder."""
    if not isinstance(message, dict):
        return None
    body = message.get('1')
    if isinstance(body, dict) and isinstance(body.get('10'), dict):
        card = _json_obj(((body.get('6') or {}).get('3') or {}).get('5'))
        chat = str(body.get('2') or '').split('@')[0]
        return chat, body['10'], card, int(body.get('5') or time.time() * 1000), 'card'
    patch = message.get('4')
    if isinstance(patch, dict) and (patch.get('extJson') or patch.get('redReminder')):
        chat = str(message.get('2') or '').split('@')[0]
        return chat, patch, {}, int(message.get('5') or time.time() * 1000), 'patch'
    skinny = message.get('3')
    if isinstance(body, str) and isinstance(skinny, dict) and skinny.get('redReminder'):
        return str(body).split('@')[0], skinny, {}, int(message.get('4') or time.time() * 1000), 'skinny'
    return None


def _button_text(meta, card):
    button = _json_obj(meta.get('_CONTENT_MAP_UPDATE_PRE_dxCard.item.main.exContent.button'))
    if button.get('text'):
        return str(button['text']).strip()
    try:
        return str(card['dxCard']['item']['main']['exContent']['button']['text']).strip()
    except (KeyError, TypeError, AttributeError):
        return ''


def _update_key(meta):
    return str(_json_obj(meta.get('extJson')).get('updateKey') or '')


def parse_trade_event(message, seller_id):
    """Platform trade cards and seller-side status patches only; never buyer text."""
    try:
        envelope = _trade_envelope(message)
        if not envelope:
            return None
        chat, meta, card, timestamp, shape = envelope
        receiver = meta.get('receiver')
        if receiver is not None and str(receiver) != str(seller_id):
            return None
        if shape == 'card' and card.get('contentType') != 26:
            return None
        ext = _json_obj(meta.get('extJson'))
        if shape == 'patch' and str(ext.get('contentType') or '') not in ('', '26'):
            return None
        if shape != 'skinny' and receiver is None:
            return None
        key = _update_key(meta).split(':') if _update_key(meta) else []
        if key and (key[0] != chat or (len(key) > 1 and not str(key[1]).isdigit())):
            return None
        marker = key[3] if len(key) >= 5 else (key[2] if len(key) >= 3 else '')
        reminder = str(meta.get('redReminder') or '')
        content = str(meta.get('reminderContent') or '')
        button = _button_text(meta, card)
        paid = (marker in _PAID_MARKERS or reminder in _PAID_REMINDERS
                or button in _PAID_BUTTONS or content in _PAID_CONTENTS)
        pending = marker == '1_not_pay_seller' or reminder == '等待买家付款'
        if paid:
            status = 'paid'
        elif pending and shape != 'skinny':
            status = 'pending'
        else:
            return None
        item = parse_qs(urlparse(str(meta.get('reminderUrl') or '')).query).get('itemId', [''])[0]
        order_id = key[1] if len(key) > 1 and str(key[1]).isdigit() else ''
        buyer = str(meta.get('senderUserId') or '')
        if shape != 'skinny' and (not order_id or not item or not buyer):
            return None
        if shape == 'skinny' and not chat:
            return None
        return dict(order_id=order_id, item_id=item, buyer_id=buyer, conversation_id=chat,
                    status=status, timestamp=timestamp)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return None


def complete_trade_event(event, store, account, order_lookup=None):
    """Fill identity for skinny paid reminders from a prior pending order."""
    if not event:
        return None
    if event.get('order_id') and event.get('item_id') and event.get('buyer_id') and event.get('conversation_id'):
        return event
    known = store.lookup_open_order(account, conversation_id=event.get('conversation_id') or None,
                                    order_id=event.get('order_id') or None)
    if known:
        return dict(event, order_id=event.get('order_id') or known['order_id'],
                    item_id=event.get('item_id') or known['item_id'],
                    buyer_id=event.get('buyer_id') or known['buyer_id'],
                    conversation_id=event.get('conversation_id') or known['conversation_id'])
    if order_lookup and event.get('order_id'):
        order = order_lookup(event['order_id']) or {}
        filled = dict(event, item_id=event.get('item_id') or order.get('item_id') or '',
                      buyer_id=event.get('buyer_id') or str(order.get('buyer_id') or ''),
                      conversation_id=event.get('conversation_id') or order.get('chat_id') or '')
        if filled.get('order_id') and filled.get('item_id') and filled.get('buyer_id'):
            return filled
    return None


class CommerceStore:
    def __init__(self, path, recover=False):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS product_automation (
                account_id TEXT, item_id TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '', bargain_enabled INTEGER NOT NULL DEFAULT 0,
                min_price TEXT NOT NULL DEFAULT '0', updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,item_id));
            CREATE TABLE IF NOT EXISTS delivery_jobs (
                account_id TEXT, order_id TEXT, item_id TEXT, buyer_id TEXT, conversation_id TEXT,
                content TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,order_id));
            CREATE TABLE IF NOT EXISTS trade_events (
                account_id TEXT, order_id TEXT, item_id TEXT, buyer_id TEXT, conversation_id TEXT,
                status TEXT NOT NULL, token TEXT NOT NULL, expires_at REAL NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                PRIMARY KEY(account_id,order_id));
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id TEXT PRIMARY KEY, account_id TEXT, conversation_id TEXT, buyer_id TEXT,
                item_id TEXT, role TEXT, content TEXT, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS conversation_lookup ON conversation_messages
                (account_id,conversation_id,buyer_id,created_at);
            CREATE TABLE IF NOT EXISTS price_changes (
                account_id TEXT, order_id TEXT, price TEXT, status TEXT, error TEXT DEFAULT '',
                updated_at REAL NOT NULL, PRIMARY KEY(account_id,order_id));
            CREATE TABLE IF NOT EXISTS catalog_snapshots (
                account_id TEXT PRIMARY KEY, item_ids TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS order_handoffs (
                account_id TEXT, order_id TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(account_id,order_id));
            ''')
            if recover:
                conn.execute("UPDATE delivery_jobs SET status='send_uncertain',error='服务重启前发送尚未完成，请人工核实' WHERE status='sending'")
                conn.execute("UPDATE delivery_jobs SET status='confirm_failed',error='服务重启前确认未完成，可重试确认发货' WHERE status='confirming'")
                conn.execute("UPDATE price_changes SET status='uncertain' WHERE status='changing'")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def config(self, account, item):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM product_automation WHERE account_id=? AND item_id=?',
                               (account, item)).fetchone()
        return dict(row) if row else dict(account_id=account, item_id=item, enabled=False, content='',
                                         bargain_enabled=False, min_price='0', updated_at=0)

    def save_catalog(self, account, ids):
        with self.db() as conn:
            conn.execute('INSERT INTO catalog_snapshots VALUES (?,?,?) ON CONFLICT(account_id) '
                         'DO UPDATE SET item_ids=excluded.item_ids,updated_at=excluded.updated_at',
                         (account, json.dumps([str(i) for i in ids]), time.time()))

    def catalog(self, account):
        with self.db() as conn:
            row = conn.execute('SELECT item_ids FROM catalog_snapshots WHERE account_id=?', (account,)).fetchone()
        return set(json.loads(row['item_ids'])) if row else None

    def import_legacy_replies(self, main_db):
        """Keep old fixed-reply deduplication when upgrading to product delivery."""
        if not Path(main_db).exists():
            return
        source = sqlite3.connect(f'file:{Path(main_db).resolve().as_posix()}?mode=ro', uri=True, timeout=2)
        try:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE name='payment_notification_replies'").fetchone():
                return
            rows = source.execute('SELECT account_id,order_id,status FROM payment_notification_replies').fetchall()
        finally:
            source.close()
        with self.db() as conn:
            for account, order, status in rows:
                conn.execute('INSERT OR IGNORE INTO delivery_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (account, order, '', '', '', '', 'legacy_sent' if status == 'sent' else 'send_uncertain',
                              '旧版本交付记录，不自动重复发送', time.time(), time.time()))

    def save_config(self, account, item, enabled, content, bargain_enabled=False, min_price='0'):
        content = content.strip()
        minimum = money(min_price)
        if enabled and not content:
            raise ValueError('启用自动发货前请填写交付内容')
        if len(content) > 10000:
            raise ValueError('交付内容不能超过10000字')
        if bargain_enabled and minimum <= 0:
            raise ValueError('启用自动改价必须设置大于0的最低成交价')
        with self.db() as conn:
            conn.execute('''INSERT INTO product_automation VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(account_id,item_id) DO UPDATE SET enabled=excluded.enabled,
                content=excluded.content,bargain_enabled=excluded.bargain_enabled,
                min_price=excluded.min_price,updated_at=excluded.updated_at''',
                (account, item, bool(enabled), content, bool(bargain_enabled), str(minimum), time.time()))
        return self.config(account, item)

    def record_message(self, account, chat, buyer, item, role, content, source_id=None):
        if role not in ('buyer', 'seller') or not content:
            return None
        now = time.time()
        msg_id = hashlib.sha256(f'{account}:{chat}:{role}:{source_id or secrets.token_hex(12)}'.encode()).hexdigest()[:32]
        with self.db() as conn:
            if not item:
                previous = conn.execute('''SELECT item_id FROM conversation_messages
                    WHERE account_id=? AND conversation_id=? AND buyer_id=? AND item_id!=''
                    ORDER BY created_at DESC LIMIT 1''', (account, chat, buyer)).fetchone()
                if not previous:
                    previous = conn.execute('''SELECT item_id FROM trade_events WHERE account_id=?
                        AND conversation_id=? AND buyer_id=? ORDER BY updated_at DESC LIMIT 1''',
                        (account, chat, buyer)).fetchone()
                item = previous['item_id'] if previous else ''
            conn.execute('INSERT OR IGNORE INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)',
                         (msg_id, account, chat, buyer, item or '', role, content, now))
        return msg_id

    def record_manual_reply(self, account, chat, text, source_id=None):
        with self.db() as conn:
            previous = conn.execute('''SELECT buyer_id,item_id FROM conversation_messages
                WHERE account_id=? AND conversation_id=? AND role='buyer'
                ORDER BY created_at DESC LIMIT 1''', (account, chat)).fetchone()
        if previous:
            return self.record_message(account, chat, previous['buyer_id'], previous['item_id'],
                                       'seller', text, source_id)

    def event(self, account, order):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM trade_events WHERE account_id=? AND order_id=?', (account, order)).fetchone()
        return dict(row) if row else None

    def lookup_open_order(self, account, conversation_id=None, order_id=None):
        with self.db() as conn:
            if order_id:
                row = conn.execute('SELECT * FROM trade_events WHERE account_id=? AND order_id=?',
                                   (account, order_id)).fetchone()
            elif conversation_id:
                row = conn.execute('''SELECT * FROM trade_events WHERE account_id=? AND conversation_id=?
                    ORDER BY updated_at DESC LIMIT 1''', (account, conversation_id)).fetchone()
            else:
                row = None
        return dict(row) if row else None

    def history(self, account, chat, buyer, limit=40):
        with self.db() as conn:
            rows = conn.execute('''SELECT id,item_id,role,content,created_at FROM conversation_messages
                WHERE account_id=? AND conversation_id=? AND buyer_id=? ORDER BY created_at DESC LIMIT ?''',
                (account, chat, buyer, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_trade(self, account, event):
        with self.db() as conn:
            old = conn.execute('SELECT * FROM trade_events WHERE account_id=? AND order_id=?',
                               (account, event['order_id'])).fetchone()
            if old and (old['buyer_id'] != event['buyer_id'] or old['item_id'] != event['item_id']
                        or old['conversation_id'] != event['conversation_id']):
                raise ValueError('订单事件身份冲突')
            status = 'paid' if event['status'] == 'paid' or (old and old['status'] == 'paid') else 'pending'
            token = old['token'] if old else secrets.token_urlsafe(32)
            conn.execute('''INSERT INTO trade_events VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,order_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at''',
                (account, event['order_id'], event['item_id'], event['buyer_id'], event['conversation_id'],
                 status, token, time.time() + 3600, 0, time.time()))
            result = dict(conn.execute('SELECT * FROM trade_events WHERE account_id=? AND order_id=?',
                                       (account, event['order_id'])).fetchone())
        return result

    def mark_notified(self, account, order):
        with self.db() as conn:
            conn.execute('UPDATE trade_events SET notified=1 WHERE account_id=? AND order_id=?', (account, order))

    def save_handoff(self, account, order, payload):
        with self.db() as conn:
            conn.execute('INSERT OR REPLACE INTO order_handoffs VALUES (?,?,?)',
                         (account, order, json.dumps(payload, ensure_ascii=False)))

    def pending_handoffs(self, account):
        with self.db() as conn:
            rows = conn.execute('''SELECT h.payload FROM order_handoffs h JOIN trade_events e
                ON h.account_id=e.account_id AND h.order_id=e.order_id WHERE e.account_id=?
                AND e.notified=0 AND e.status='pending' AND e.expires_at>? ORDER BY e.updated_at''',
                (account, time.time())).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def authorize(self, token):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM trade_events WHERE token=?', (token,)).fetchone()
        if not row or row['expires_at'] < time.time():
            raise ValueError('订单操作凭证不存在或已过期，请等待新的订单事件')
        return dict(row)

    def jobs(self, accounts, limit=100):
        if not accounts:
            return []
        with self.db() as conn:
            rows = conn.execute(f'''SELECT account_id,order_id,item_id,buyer_id,status,error,updated_at
                FROM delivery_jobs WHERE account_id IN ({','.join('?' for _ in accounts)})
                ORDER BY updated_at DESC LIMIT ?''', (*accounts, limit)).fetchall()
        return [dict(row) for row in rows]

    async def deliver(self, account, event, send, confirm):
        if event.get('status') != 'paid':
            return 'not_paid'
        config = self.config(account, event['item_id'])
        with self.db() as conn:
            job = conn.execute('SELECT * FROM delivery_jobs WHERE account_id=? AND order_id=?',
                               (account, event['order_id'])).fetchone()
            if not job:
                if not config['enabled'] or not config['content']:
                    return 'not_configured'
                conn.execute('INSERT OR IGNORE INTO delivery_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (account, event['order_id'], event['item_id'], event['buyer_id'],
                              event['conversation_id'], config['content'], 'ready', '', time.time(), time.time()))
            claimed = conn.execute("UPDATE delivery_jobs SET status='sending',updated_at=? WHERE account_id=? AND order_id=? AND status='ready'",
                                   (time.time(), account, event['order_id'])).rowcount
            job = dict(conn.execute('SELECT * FROM delivery_jobs WHERE account_id=? AND order_id=?',
                                    (account, event['order_id'])).fetchone())
        if claimed:
            try:
                await send(job['content'])
            except BaseException:
                self._job_state(account, event['order_id'], 'send_uncertain', '发送结果不确定，请人工核实，系统不会自动重发')
                raise
            self._job_state(account, event['order_id'], 'sent')
        elif job['status'] not in ('sent', 'confirm_failed'):
            return job['status']
        # Claim confirmation too: replayed events may retry only the confirmation.
        with self.db() as conn:
            claimed = conn.execute("UPDATE delivery_jobs SET status='confirming' WHERE account_id=? AND order_id=? AND status IN ('sent','confirm_failed')",
                                   (account, event['order_id'])).rowcount
        if not claimed:
            return 'in_progress'
        try:
            result = await confirm()
            if not isinstance(result, dict) or result.get('success') is not True:
                self._job_state(account, event['order_id'], 'confirm_failed', '交付内容已发送，无需物流确认失败；可安全重试确认')
                return 'confirm_failed'
        except Exception:
            self._job_state(account, event['order_id'], 'confirm_failed', '交付内容已发送，确认请求异常；可安全重试确认')
            return 'confirm_failed'
        self._job_state(account, event['order_id'], 'completed')
        return 'completed'

    def _job_state(self, account, order, state, error=''):
        with self.db() as conn:
            conn.execute('UPDATE delivery_jobs SET status=?,error=?,updated_at=? WHERE account_id=? AND order_id=?',
                         (state, error, time.time(), account, order))

    def validate_price(self, token, price, seller_message_id, buyer_message_id, settings, original_price):
        event = self.authorize(token)
        if event['status'] != 'pending':
            raise ValueError('订单已经付款，不允许改价')
        config = self.config(event['account_id'], event['item_id'])
        if not config['bargain_enabled']:
            raise ValueError('该商品未启用自动改价')
        new, original = money(price), money(original_price)
        if original <= 0 or new <= 0 or new >= original:
            raise ValueError('只允许将未付款订单降价到大于0且低于原价的金额')
        percent = Decimal(str(settings.get('max_discount_percent', 0)))
        amount = Decimal(str(settings.get('max_discount_amount', 0)))
        if not percent.is_finite() or not amount.is_finite() or not 0 <= percent <= 100 or amount < 0:
            raise ValueError('账号优惠比例或金额配置无效')
        floor = max(money(config['min_price']), original * (1 - percent / 100), original - amount)
        if new < floor:
            raise ValueError(f'成交价低于允许底价 {floor.quantize(Decimal(".01"))} 元')
        history = self.history(event['account_id'], event['conversation_id'], event['buyer_id'], 100)
        evidence = {m['id']: m for m in history}
        seller, buyer = evidence.get(seller_message_id), evidence.get(buyer_message_id)
        if not seller or not buyer or seller['role'] != 'seller' or buyer['role'] != 'buyer':
            raise ValueError('必须提供真实卖家报价及买家确认的消息ID')
        if seller['item_id'] != event['item_id'] or buyer['item_id'] != event['item_id']:
            raise ValueError('议价记录商品不匹配')
        if min(seller['created_at'], buyer['created_at']) < time.time() - 86400:
            raise ValueError('议价记录超过24小时')
        if not quoted_seller_price(seller['content'], new):
            raise ValueError('卖家记录没有明确同意本次成交金额，或存在歧义报价')
        text = buyer['content'].strip()
        if re.search(r'不行|不同意|不可以|不要|太贵|再便宜|不接受|不买', text):
            raise ValueError('买家尚未同意报价')
        buyer_prices = re.findall(_PRICE_NUMBER, text)
        if buyer_prices and {money(p) for p in buyer_prices} != {new}:
            raise ValueError('买家确认的金额与卖家报价不一致')
        explicit_accept = bool(re.search(r'^(好[的啊呀]?|行[啊的]?|可以|成交|同意|就这个价|按这个价)([，,。！!～~\s]|$)', text))
        explicit_price = bool(buyer_prices) and {money(p) for p in buyer_prices} == {new}
        order_intent = bool(re.search(r'拍下|下单|我拍|拍了|改价|改价格|改成|按这个价|成交', text))
        if buyer['created_at'] >= seller['created_at']:
            if not (explicit_accept or (explicit_price and order_intent)):
                raise ValueError('没有明确的买家同意记录，请先让买家确认报价')
        else:
            # Buyer proposes/accepts the exact price first; seller explicitly
            # confirms it afterwards. A generic earlier '好的' is not evidence.
            seller_accept = bool(re.search(r'可以|没问题|成交|同意|给你|给您', seller['content']))
            if not (explicit_price and seller_accept and re.search(r'拍|下单|改|卖不卖|可以|行不行|成交', text)):
                raise ValueError('买家消息早于卖家报价，且缺少双方明确同意的同价记录')
        return event, new

    def claim_price(self, account, order, price):
        with self.db() as conn:
            inserted = conn.execute('INSERT OR IGNORE INTO price_changes VALUES (?,?,?,?,?,?)',
                                    (account, order, str(price), 'changing', '', time.time())).rowcount
            if inserted:
                return True
            return bool(conn.execute("UPDATE price_changes SET status='changing',price=?,updated_at=? WHERE account_id=? AND order_id=? AND status='failed'",
                                     (str(price), time.time(), account, order)).rowcount)

    def finish_price(self, account, order, success, retry_safe=False):
        with self.db() as conn:
            conn.execute('UPDATE price_changes SET status=?,updated_at=? WHERE account_id=? AND order_id=?',
                         ('completed' if success else ('failed' if retry_safe else 'uncertain'), time.time(), account, order))


_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                candidate = CommerceStore(os.getenv('COMMERCE_DB_PATH', 'data/commerce_automation.db'), recover=True)
                candidate.import_legacy_replies(os.getenv('DB_PATH', 'data/xianyu_data.db'))
                _store = candidate
    return _store
