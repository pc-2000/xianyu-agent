"""Build the non-secret context delivered to the OpenClaw channel."""

import hashlib
import json
import time


def build_bridge_message(account_id, chat_id, sender_id, sender_name, content,
                         item_id, source, item=None, settings=None):
    fingerprint = hashlib.sha256(json.dumps(
        source, sort_keys=True, ensure_ascii=False, default=str
    ).encode('utf-8')).hexdigest()[:24]
    item = item or {}
    settings = settings or {}
    return {
        'messageId': f'{account_id}:{chat_id}:{fingerprint}',
        'accountId': account_id,
        'conversationId': chat_id,
        'senderId': sender_id,
        'senderName': sender_name,
        'content': content,
        'contentType': 'text',
        'itemId': item_id,
        'timestamp': int(time.time() * 1000),
        'replyContext': {
            'product': {
                'title': item.get('item_title', ''),
                'price': item.get('item_price', ''),
                'category': item.get('item_category', ''),
                'description': item.get('item_detail') or item.get('item_description', ''),
            },
            'sellerInstructions': settings.get('custom_prompts', ''),
            'maxDiscountPercent': settings.get('max_discount_percent', 10),
            'maxDiscountAmount': settings.get('max_discount_amount', 100),
            'maxBargainRounds': settings.get('max_bargain_rounds', 3),
        },
    }
