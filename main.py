import os
import shutil
import asyncio
import json
import base64
import requests
import time
import uuid
import urllib.parse
import io
import tempfile
import random
import redis.asyncio as redis 
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "http://localhost:8080")
ADMIN_IDS = [int(aid.strip()) for aid in os.environ.get("ADMIN_ID", "0").split(",") if aid.strip().isdigit()]

PHONE, OTP, ASK_NAME, ASK_TAG, ASK_SEARCH = range(5)

# محدود کردن Worker ها
executor = ThreadPoolExecutor(max_workers=5)

# لیست User-Agent های واقعی موبایل
USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Mobile Safari/537.36"
]

def get_anti_bot_headers():
    return {
        'accept': 'application/json, text/plain, */*',
        'source': 'okala',
        'ui-version': '2.0',
        'origin': 'https://www.okala.com',
        'User-Agent': random.choice(USER_AGENTS),
        'X-User-Unique-Id': str(uuid.uuid4()), 
        'X-Correlation-Id': str(uuid.uuid4()),
        'session-id': str(uuid.uuid4())
    }

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ==========================================
# سیستم مدیریت پروکسی و API
# ==========================================
async def get_random_proxy_from_db():
    proxies_json = await redis_client.get("settings:proxies")
    if proxies_json:
        proxies = json.loads(proxies_json)
        if proxies and len(proxies) > 0:
            p = random.choice(proxies)
            return {"http": p, "https": p}
    return None

def get_user_id_from_token(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded_bytes)
        uid = data.get('cerberusId') or data.get('alternativeCustomerId') or data.get('userId')
        return int(uid) if uid else 0
    except Exception:
        return 0

def update_tokens_in_data(data, old_acc, new_acc, old_ref, new_ref):
    try:
        content = json.dumps(data, ensure_ascii=False)
        if old_acc and new_acc: content = content.replace(old_acc, new_acc)
        if old_ref and new_ref: content = content.replace(old_ref, new_ref)
        return json.loads(content)
    except Exception:
        return data

class OkalaAPI:
    def __init__(self):
        self.request_logs = []
        self.base_headers = {
            'accept': 'application/json, text/plain, */*',
            'source': 'okala',
            'ui-version': '2.0',
            'origin': 'https://www.okala.com',
            'User-Agent': random.choice(USER_AGENTS),
            'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?1',
            'sec-ch-ua-platform': '"Android"',
        }

    def log_request(self, method, url, status_code, response_text):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.request_logs.append(f"[{timestamp}] {method} {url}\nStatus: {status_code}\nResponse: {response_text}\n{'-'*50}\n")

    def make_request(self, method, url, access_token=None, proxy_dict=None, **kwargs):
        headers = self.base_headers.copy()
        headers['X-Correlation-Id'] = str(uuid.uuid4())
        headers['X-User-Unique-Id'] = str(uuid.uuid4())
        headers['session-id'] = str(uuid.uuid4())
        if access_token: headers['Authorization'] = f'Bearer {access_token}'
        if 'headers' in kwargs: headers.update(kwargs.pop('headers'))

        for attempt in range(3):
            try:
                time.sleep(random.uniform(1.0, 2.5))
                res = requests.request(method, url, headers=headers, proxies=proxy_dict, timeout=25, **kwargs)
                self.log_request(method, url, res.status_code, res.text)
                if res.status_code == 200:
                    try: return 200, res.json()
                    except: return 200, {}
                elif res.status_code == 401: return 401, {}
                else: return res.status_code, res.text 
            except Exception as e:
                self.log_request(method, url, "EXCEPTION", str(e))
                time.sleep(1)
        return 0, "Network Error"

    def check_discount_api(self, token, uid, proxy_dict=None):
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{uid}"
        return self.make_request('GET', url, access_token=token, proxy_dict=proxy_dict)

    def refresh_token(self, refresh_token, proxy_dict=None):
        url = "https://apigateway.okala.com/api/v1/accounts/tokens"
        data = {"grant_type": "refresh_token", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "scope": "offline_access", "refresh_token": refresh_token}
        status, response_data = self.make_request('POST', url, headers={"content-type": "application/x-www-form-urlencoded"}, data=data, proxy_dict=proxy_dict)
        if status == 200 and isinstance(response_data, dict):
            return response_data.get('access_token'), response_data.get('refresh_token')
        return None, None

# ==========================================
# پردازش تخفیف‌ها از دیتابیس
# ==========================================
async def process_discounts_and_send_report(bot, chat_id, acc_keys):
    loop = asyncio.get_running_loop()
    api = OkalaAPI()
    ts = int(time.time())

    # بررسی وجود پروکسی
    proxy_check = await get_random_proxy_from_db()
    if not proxy_check:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ <b>هیچ پروکسی‌ای در سیستم تنظیم نشده است!</b>\n"
                 "بررسی تخفیف بدون پروکسی ادامه می‌یابد — ممکن است نتایج نادرست باشد.\n"
                 "برای تنظیم پروکسی از پنل ادمین اقدام کنید.",
            parse_mode='HTML'
        )

    # استخراج لینک‌های ثبت‌شده از لاگ دیتابیس
    raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
    phone_to_latest_link = {}
    for item in raw_logs:
        try:
            entry = json.loads(item)
            phone_to_latest_link[entry['phone']] = entry['link']
        except:
            pass

    total = len(acc_keys)
    progress_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"🔍 شروع بررسی <b>{total}</b> اکانت با پروکسی...\n⏳ لطفاً منتظر بمانید.",
        parse_mode='HTML'
    )

    # لاگ تفصیلی هر بررسی
    detail_logs = []

    def _check_sync(acc_token, ref_token, uid, p_dict, phone):
        proxy_ip = p_dict['http'].split('@')[-1].split(':')[0] if p_dict else "بدون پروکسی"
        log_line = f"[{time.strftime('%H:%M:%S')}] 📱 {phone} | UUID: {uid} | پروکسی: {proxy_ip}\n"

        status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)
        refreshed = False

        if status == 401 and ref_token:
            log_line += f"  ♻️ توکن منقضی — در حال رفرش...\n"
            new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
            if new_acc:
                status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                refreshed = True
                log_line += f"  ✅ رفرش موفق — بررسی مجدد انجام شد.\n"
                return status, res, new_acc, new_ref, log_line
            else:
                log_line += f"  ❌ رفرش ناموفق.\n"
        
        if status == 200 and isinstance(res, dict):
            vouchers = res.get('data', [])
            amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
            if vouchers:
                log_line += f"  🎁 تخفیف یافت شد: {len(vouchers)} کد | بیشترین مبلغ: {max(amounts)//10000 if amounts else '?'} هزار تومان\n"
            else:
                log_line += f"  ➖ بدون تخفیف (پاسخ 200)\n"
        elif status == 401:
            log_line += f"  🔒 توکن کاملاً منقضی شده.\n"
        else:
            log_line += f"  ❌ خطا — کد پاسخ: {status}\n"

        return status, res, None, None, log_line

    # پردازش اکانت‌ها
    discount_results = []
    done = 0

    for key in acc_keys:
        try:
            phone = key.replace("account:", "")
            token_data = await redis_client.hgetall(key)
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")

            if not access_token:
                detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ⚠️ {phone} — توکن موجود نیست، رد شد.\n")
                done += 1
                continue

            user_uuid = get_user_id_from_token(access_token)
            if not user_uuid:
                detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ⚠️ {phone} — UUID قابل استخراج نیست، رد شد.\n")
                done += 1
                continue

            proxy_dict = await get_random_proxy_from_db()

            status, res, new_acc, new_ref, log_line = await loop.run_in_executor(
                executor, _check_sync, access_token, refresh_token, user_uuid, proxy_dict, phone
            )
            detail_logs.append(log_line)

            if new_acc:
                await redis_client.hset(key, mapping={"access_token": new_acc, "refresh_token": new_ref or ""})

            if status == 200 and isinstance(res, dict):
                vouchers = res.get('data', [])
                if vouchers:
                    amounts = [v.get('discountAmount', 0) for v in vouchers if v.get('discountAmount')]
                    max_amount = max(amounts) // 10000 if amounts else 0
                    old_link = phone_to_latest_link.get(phone, "")
                    discount_results.append({
                        "phone": phone,
                        "count": len(vouchers),
                        "max_amount": max_amount,
                        "link": old_link
                    })

            done += 1
            # آپدیت پیشرفت هر ۵ اکانت یا آخرین اکانت
            if done % 5 == 0 or done == total:
                try:
                    await progress_msg.edit_text(
                        f"🔍 بررسی اکانت‌ها...\n"
                        f"✅ انجام شده: <b>{done}/{total}</b>\n"
                        f"🎁 دارای تخفیف تاکنون: <b>{len(discount_results)}</b>",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass

        except Exception as e:
            detail_logs.append(f"[{time.strftime('%H:%M:%S')}] ❌ خطای کلی برای {key}: {e}\n")
            logging.error(f"Discount check error for {key}: {e}")

    # ساخت گزارش نهایی
    if discount_results:
        report_text = f"🎁 <b>گزارش بررسی تخفیف‌ها ({len(discount_results)} اکانت دارای تخفیف از {total}):</b>\n\n"
        for r in discount_results:
            link_line = f"🔗 {r['link']}" if r['link'] else "⚠️ لینک ثبت‌شده‌ای در دیتابیس یافت نشد"
            report_text += (
                f"📱 شماره: <code>{r['phone']}</code>\n"
                f"🎟 تعداد کد تخفیف: <b>{r['count']}</b> | بیشترین مبلغ: <b>{r['max_amount']} هزار تومان</b>\n"
                f"{link_line}\n"
                f"{'─'*30}\n"
            )
    else:
        report_text = f"➖ <b>هیچ تخفیفی یافت نشد.</b>\nتعداد کل اکانت‌های بررسی‌شده: {total}"

    # ارسال گزارش تخفیف‌ها
    try:
        await progress_msg.delete()
    except Exception:
        pass

    try:
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await bot.send_document(
                chat_id=chat_id, document=file_out,
                filename=f"Discounts_Report_{ts}.txt",
                caption=f"✅ گزارش کامل تخفیف‌ها — {len(discount_results)} اکانت دارای تخفیف"
            )
        else:
            await bot.send_message(chat_id=chat_id, text=report_text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Error sending discount report: {e}")

    # ارسال لاگ تفصیلی
    try:
        full_log = f"=== لاگ بررسی تخفیف | {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        full_log += f"کل اکانت‌ها: {total} | دارای تخفیف: {len(discount_results)}\n"
        full_log += "=" * 50 + "\n\n"
        full_log += "".join(detail_logs)
        full_log += "\n\n=== لاگ درخواست‌های HTTP ===\n"
        full_log += "".join(api.request_logs)

        log_out = io.BytesIO(full_log.encode('utf-8'))
        await bot.send_document(
            chat_id=chat_id, document=log_out,
            filename=f"Discount_Full_Log_{ts}.txt",
            caption=f"📄 لاگ کامل بررسی ({total} اکانت)"
        )
    except Exception as e:
        logging.error(f"Error sending log file: {e}")

# ==========================================
# تبدیل دیتا برای وب
# ==========================================
def format_for_injector(auth_data):
    access_token = auth_data.get("access_token", "")
    refresh_token = auth_data.get("refresh_token", "")
    user_info = auth_data.get("UserInfo", {})
    
    user_dict = {
        "id": user_info.get("Id", 0), "alternativeId": user_info.get("AlternativeId", ""), "alternativeCustomerId": user_info.get("AlternativeCustomerId", 0),
        "firstName": user_info.get("FirstName", ""), "lastName": user_info.get("LastName", ""), "birthDate": "", "genderCode": user_info.get("GenderCode", 1),
        "emailAddress": user_info.get("EmailAddress", ""), "userName": user_info.get("UserName", ""), "mobilePhone": user_info.get("MobilePhone", ""),
        "stateCode": user_info.get("StateCode", 1), "customerIsLoggedInForFirstTime": user_info.get("CustomerIsLoggedInForFirstTime", False),
        "firstLoginDateTime": user_info.get("FirstLoginDateTime", ""), "state": user_info.get("State", False),
        "hasAddress": user_info.get("HasAddress", False), "birthDateEpoch": user_info.get("BirthDateEpoch", 0)
    }
    
    user_url_encoded = urllib.parse.quote(json.dumps(user_dict, ensure_ascii=False))
    persist_user_inner = user_dict.copy()
    persist_user_inner["token"] = access_token
    
    persist_root_dict = {
        "user": json.dumps({"user": persist_user_inner, "discountCode": None}, ensure_ascii=False),
        "cart": json.dumps({"cartData": [], "totalCartsCount": 0, "showDrawer": False, "cartTotalPrice": 0}),
        "mapInfo": json.dumps({"defaultViewPort": {"latitude": 35.69976, "longitude": 51.33808, "id": 129, "name": "تهران"}, "viewport": {"latitude": 35.69976, "longitude": 51.33808}, "selectedCity": {"id": 129, "name": "تهران", "lat": 35.69975, "lng": 51.33551}, "mapCityName": "تهران"}, ensure_ascii=False),
        "eventData": json.dumps({"isLoggedIn": True, "platform": "web", "viewedLayersCount": 0, "activeDiscountCodesCount": 0, "sessionLayersViewedCount": 0}),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    
    persist_root_str = json.dumps(persist_root_dict, ensure_ascii=False)
    
    return {
        "cookies": [
            {"name": "tokenMS", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "token", "value": access_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"},
            {"name": "refresh_token", "value": refresh_token, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"}
        ],
        "origins": [{
            "origin": "https://www.okala.com",
            "localStorage": [
                {"name": "tokenMS", "value": access_token}, {"name": "user", "value": user_url_encoded},
                {"name": "city_name", "value": "تهران"}, {"name": "city_id", "value": "129"},
                {"name": "persist:root", "value": persist_root_str}
            ]
        }]
    }

# ==========================================
# پردازش فایل زیپ و بررسی تخفیف
# ==========================================
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ فایل ارسالی نامعتبر است. لطفاً فایل زیپ (.zip) ارسال کنید.")
        return
        
    action = context.user_data.get('admin_zip_action', 'zip_to_link')
    msg = await update.message.reply_text("⏳ در حال دریافت و استخراج فایل...")
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    
    new_file = await update.message.document.get_file()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        await new_file.download_to_drive(zip_path)
        
        extracted_dir = os.path.join(temp_dir, "extracted")
        await asyncio.to_thread(shutil.unpack_archive, zip_path, extracted_dir)
        
        json_files_paths = []
        for root, dirs, files in os.walk(extracted_dir):
            for file in files:
                if file.lower().endswith('.json'):
                    json_files_paths.append(os.path.join(root, file))
                    
        if not json_files_paths:
            await msg.edit_text("⚠️ هیچ فایل JSON معتبری در فایل زیپ یافت نشد.")
            return

        if action == 'zip_to_link':
            links_text = "<b>لیست لینک‌های تولید شده:</b>\n\n"
            count = 0
            for file_path in json_files_paths:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token, refresh_token = None, None
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                            elif cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                                    elif item.get('name') == 'refresh_token': refresh_token = item.get('value')
                        phone = filename.replace('.json', '')
                        if access_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token or ""})
                        link_id = str(uuid.uuid4())[:12]
                        await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        links_text += f"📱 <b>شماره {phone}:</b>\n{final_url}\n\n"
                        count += 1
                except Exception:
                    pass
            if len(links_text) > 4000:
                file_out = io.BytesIO(links_text.encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Links_{int(time.time())}.txt", caption=f"✅ استخراج {count} اکانت انجام شد.")
                await msg.delete()
            else:
                await msg.edit_text(f"✅ <b>تعداد {count} اکانت ذخیره شد:</b>\n\n{links_text}", disable_web_page_preview=True, parse_mode='HTML')

        elif action == 'zip_discount_check':
            await msg.edit_text("🔍 در حال بررسی وضعیت تخفیف‌ها با سیستم ضدربات و رفرش‌توکن. لطفاً منتظر بمانید...")
            discount_dir = os.path.join(temp_dir, "Discount_Accounts")
            os.makedirs(os.path.join(discount_dir, 'accounts'), exist_ok=True)
            links_text = "<b>لیست لینک‌های دارای تخفیف:</b>\n\n"
            discount_count = 0
            
            api = OkalaAPI()
            loop = asyncio.get_running_loop()

            raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
            phone_to_latest_link = {}
            for item in raw_logs:
                try:
                    entry = json.loads(item)
                    phone_to_latest_link[entry['phone']] = entry['link']
                except: pass
            
            def _check_sync_zip(acc_token, ref_token, uid, p_dict):
                status, res = api.check_discount_api(acc_token, uid, proxy_dict=p_dict)
                if status == 401 and ref_token:
                    new_acc, new_ref = api.refresh_token(ref_token, proxy_dict=p_dict)
                    if new_acc:
                        status, res = api.check_discount_api(new_acc, uid, proxy_dict=p_dict)
                        return status, res, new_acc, new_ref
                return status, res, None, None
            
            for file_path in json_files_paths:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token = None
                        refresh_token = None
                        phone = filename.replace('.json', '')
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                            if cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                                    if item.get('name') == 'refresh_token': refresh_token = item.get('value')
                        
                        if access_token:
                            user_uuid = get_user_id_from_token(access_token)
                            if user_uuid:
                                proxy_dict = await get_random_proxy_from_db()
                                status, res, new_acc, new_ref = await loop.run_in_executor(
                                    executor, _check_sync_zip, access_token, refresh_token, user_uuid, proxy_dict
                                )
                                
                                if new_acc:
                                    data = update_tokens_in_data(data, access_token, new_acc, refresh_token, new_ref)
                                    file_content = json.dumps(data, ensure_ascii=False)
                                    with open(file_path, 'w', encoding='utf-8') as fw:
                                        fw.write(file_content)

                                if status == 200 and isinstance(res, dict):
                                    vouchers = res.get('data', [])
                                    if vouchers:
                                        discount_count += 1
                                        shutil.copy2(file_path, os.path.join(discount_dir, 'accounts', filename))
                                        
                                        old_link = phone_to_latest_link.get(phone, "لینک قدیمی در دیتابیس یافت نشد")
                                        links_text += f"📱 <b>شماره {phone}:</b>\n{old_link}\n\n"
                                    
                except Exception as e:
                    api.request_logs.append(f"[{filename}] Exception: {str(e)}\n{'-'*40}\n")
                    
            debug_logs = api.request_logs
            
            if discount_count > 0:
                discount_zip_path = os.path.join(temp_dir, "Discounted_Accounts")
                await asyncio.to_thread(shutil.make_archive, discount_zip_path, 'zip', discount_dir)
                await msg.delete()
                with open(discount_zip_path + '.zip', 'rb') as zip_file:
                    await context.bot.send_document(chat_id=user_id, document=zip_file, filename="Discounted_Accounts.zip", caption=f"🎁 <b>فایل خروجی (فیلتر شده)</b>\nتعداد اکانت‌های دارای تخفیف: {discount_count}", parse_mode='HTML')
                if len(links_text) > 4000:
                    file_out = io.BytesIO(links_text.encode('utf-8'))
                    await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Discount_Links_{int(time.time())}.txt", caption="🔗 لینک‌های دسترسی سریع")
                else:
                    await context.bot.send_message(chat_id=user_id, text=links_text, disable_web_page_preview=True, parse_mode='HTML')
            else:
                await msg.edit_text("⚠️ هیچ‌یک از اکانت‌های موجود دارای تخفیف نبودند.")
                
            if debug_logs:
                debug_out = io.BytesIO("".join(debug_logs).encode('utf-8'))
                await context.bot.send_document(chat_id=user_id, document=debug_out, filename=f"Discount_Debug_Log_{int(time.time())}.txt", caption="📄 فایل لاگ پاسخ درخواست‌های سرور")

# ==========================================
# مینی‌سرور وب
# ==========================================
async def web_handler_get_account(request):
    link_id = request.match_info.get('link_id', '')
    data = await redis_client.get(f"acc_link:{link_id}")
    if data:
        return web.json_response(json.loads(data))
    return web.json_response({"error": "لینک نامعتبر است یا منقضی شده."}, status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/acc/{link_id}', web_handler_get_account)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ==========================================
# منوها و دکمه‌ها
# ==========================================
def get_main_keyboard(is_admin_user, active_tag_name=None):
    keyboard = [[InlineKeyboardButton("🔑 ورود به حساب", callback_data="user_login")]]
    
    tag_btn_text = f"🏷 تغییر/حذف برچسب (فعال: {active_tag_name})" if active_tag_name else "🏷 تنظیم برچسب نشست (Tag)"
    keyboard.append([InlineKeyboardButton(tag_btn_text, callback_data="set_tag")])
    
    keyboard.append([
        InlineKeyboardButton("📂 برچسب‌های من", callback_data="my_tags"),
        InlineKeyboardButton("🔍 جستجوی لینک", callback_data="search_links")
    ])
    
    keyboard.append([InlineKeyboardButton("📞 تماس با مدیر", callback_data="contact_admin")])
    
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data="admin_stats"), InlineKeyboardButton("⏳ تنظیم انقضا", callback_data="admin_expire")],
        [InlineKeyboardButton("📋 گزارش لینک‌های کاربران", callback_data="admin_users_report")],
        [InlineKeyboardButton("🎁 بررسی تخفیف دیتابیس", callback_data="admin_check_discounts")],
        [InlineKeyboardButton("🔗 تبدیل زیپ به لینک", callback_data="admin_zip_to_link"), InlineKeyboardButton("🔍 بررسی تخفیف زیپ", callback_data="admin_zip_discount")],
        [InlineKeyboardButton("📥 استخراج شماره‌ها", callback_data="admin_export"), InlineKeyboardButton("🗑 پاکسازی", callback_data="admin_clear")],
        [InlineKeyboardButton("🔗 استخراج لینک‌ها", callback_data="admin_export_links"), InlineKeyboardButton("🔑 استخراج توکن‌ها", callback_data="admin_export_tokens")],
        [InlineKeyboardButton("🛠 تعمیر لینک‌های ناقص (سریع)", callback_data="admin_repair_links")],
        [InlineKeyboardButton("🌐 تنظیم پروکسی", callback_data="admin_set_proxy")], 
        [InlineKeyboardButton("⏸ روشن/خاموش کردن", callback_data="admin_toggle")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_status = is_admin(user_id)
    active_tag = context.user_data.get('active_tag_name')
    text = "👋 <b>به سیستم لینک ساز خوش آمدید.</b>\n\nلطفاً یک گزینه را انتخاب کنید:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=get_main_keyboard(admin_status, active_tag), parse_mode='HTML')
    else:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard(admin_status, active_tag), parse_mode='HTML')
        except Exception:
            pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    context.user_data['admin_state'] = None
    context.user_data['admin_zip_action'] = None
    await update.message.reply_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')

# ==========================================
# توابع مربوط به برچسب‌گذاری (Tagging) و جستجو
# ==========================================
async def ask_tag_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]]
    if context.user_data.get('active_tag_name'):
        kb.insert(0, [InlineKeyboardButton("🗑 پاک کردن برچسب فعلی", callback_data="clear_active_tag")])
        
    text = (
        "🏷 <b>تنظیم برچسب (Batch Tagging)</b>\n\n"
        "لطفاً یک نام برای این دسته ارسال کنید (مثلاً: <code>مشتری 1</code> یا <code>سفارش صبح</code>).\n"
        "تمامی لینک‌هایی که از این پس ساخته شوند تحت این برچسب در بخش «برچسب‌های من» ذخیره خواهند شد."
    )
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    return ASK_TAG

async def receive_tag_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tag_name = update.message.text.strip()
    tag_id = uuid.uuid4().hex[:8]
    context.user_data['active_tag_id'] = tag_id
    context.user_data['active_tag_name'] = tag_name
    
    await update.message.reply_text(
        f"✅ برچسب <b>«{tag_name}»</b> با موفقیت تنظیم شد.\nاکنون می‌توانید شروع به ساختن لینک کنید.", 
        parse_mode='HTML', 
        reply_markup=get_main_keyboard(is_admin(update.effective_user.id), tag_name)
    )
    return ConversationHandler.END

async def clear_active_tag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('active_tag_id', None)
    context.user_data.pop('active_tag_name', None)
    await update.callback_query.answer("برچسب پاک شد 🗑")
    await show_main_menu(update, context)
    return ConversationHandler.END

async def ask_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_action")]])
    text = (
        "🔍 <b>جستجوی سریع لینک‌ها</b>\n\n"
        "لطفاً شماره موبایل کامل یا <b>۴ رقم آخر</b> آن‌ها را ارسال کنید.\n"
        "می‌توانید در هر خط یک شماره بفرستید تا ربات همه را با هم جستجو کند.\n\n"
        "<b>مثال:</b>\n"
        "<code>09123456789\n5678\n09351112222</code>"
    )
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
    return ASK_SEARCH

async def receive_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    queries = update.message.text.strip().split('\n')
    queries = [q.strip() for q in queries if q.strip()]
    
    msg = await update.message.reply_text("⏳ در حال جستجو در دیتابیس...")
    
    raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
    phone_to_latest_link = {}
    
    for item in raw_logs:
        try:
            entry = json.loads(item)
            phone_to_latest_link[entry['phone']] = entry['link']
        except: pass
        
    report_text = "🔍 <b>نتایج جستجوی شما:</b>\n\n"
    found_count = 0
    
    for q in queries:
        found_for_q = False
        for phone, link in phone_to_latest_link.items():
            if q == phone or (len(q) >= 4 and phone.endswith(q)):
                report_text += f"✅ <code>{phone}</code>\n🔗 {link}\n\n"
                found_for_q = True
                found_count += 1
        if not found_for_q:
            report_text += f"❌ <code>{q}</code> ➔ یافت نشد\n\n"
            
    if len(report_text) > 4000:
        file_out = io.BytesIO(report_text.encode('utf-8'))
        await context.bot.send_document(chat_id=update.effective_user.id, document=file_out, filename=f"Search_Results_{int(time.time())}.txt", caption=f"✅ جستجو پایان یافت. پیدا شده: {found_count}")
        await msg.delete()
    else:
        await msg.edit_text(report_text, parse_mode='HTML', disable_web_page_preview=True)
        
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==========================================
# مدیریت دکمه‌های اصلی و ادمین
# ==========================================
async def core_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if data == "main_menu":
        await query.answer()
        context.user_data['admin_state'] = None
        await show_main_menu(update, context)
        return
        
    if data == "contact_admin":
        await query.answer()
        await query.edit_message_text(
            "📞 <b>ارتباط با مدیریت:</b>\n\n"
            "جهت هرگونه سوال، پیشنهاد یا گزارش مشکل به آیدی زیر پیام دهید:\n"
            "@NAVLINK_1",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]]),
            parse_mode='HTML'
        )
        return
        
    if data == "finish_link_creation":
        await query.answer("در حال آماده‌سازی لینک‌های شما... ⏳")
        session_links = context.user_data.get('session_links', [])
        active_tag_name = context.user_data.get('active_tag_name')
        active_tag_id = context.user_data.get('active_tag_id')
        
        if not session_links:
            await query.edit_message_text("⚠️ هیچ لینکی در این نوبت ساخته نشده است.", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
            return
        
        report_text = f"🎉 <b>لینک‌های تولید شده شما (تعداد: {len(session_links)}):</b>\n"
        
        if active_tag_name and active_tag_id:
            report_text += f"🏷 <b>ذخیره شده در برچسب:</b> {active_tag_name}\n\n"
            tag_meta_key = f"user_tag_meta:{user_id}:{active_tag_id}"
            await redis_client.set(tag_meta_key, active_tag_name)
            await redis_client.sadd(f"user_tags_set:{user_id}", active_tag_id)
            
            tag_links_key = f"user_tag_links:{active_tag_id}"
            for item in session_links:
                await redis_client.rpush(tag_links_key, json.dumps(item, ensure_ascii=False))
        else:
            report_text += "\n"
            
        for idx, item in enumerate(session_links, 1):
            report_text += f"{idx}. 📱 <b>شماره:</b> <code>{item['phone']}</code>\n🔗 {item['link']}\n\n"
        
        context.user_data['session_links'] = []
        
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"My_Links_{int(time.time())}.txt", caption="✅ لینک‌های ساخته شده شما")
            await query.message.delete()
            await context.bot.send_message(chat_id=user_id, text="بازگشت به منوی اصلی:", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML')
            await context.bot.send_message(chat_id=user_id, text="بازگشت به منوی اصلی:", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
        return

    if data == "my_tags":
        await query.answer()
        tag_ids = await redis_client.smembers(f"user_tags_set:{user_id}")
        active_tag_name = context.user_data.get('active_tag_name')
        
        if not tag_ids:
            await query.edit_message_text("⚠️ شما هنوز هیچ برچسبی ایجاد نکرده‌اید.", reply_markup=get_main_keyboard(is_admin(user_id), active_tag_name))
            return
            
        kb = []
        for tid in list(tag_ids)[:40]: 
            t_name = await redis_client.get(f"user_tag_meta:{user_id}:{tid}")
            if t_name:
                kb.append([InlineKeyboardButton(f"🏷 {t_name}", callback_data=f"show_tag_{tid}")])
                
        kb.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")])
        await query.edit_message_text("📂 <b>برچسب‌های ذخیره‌شده شما:</b>\nبرای مشاهده و دریافت لینک‌ها روی برچسب مورد نظر کلیک کنید:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        return

    if data.startswith("show_tag_"):
        await query.answer()
        tid = data.split("show_tag_")[1]
        t_name = await redis_client.get(f"user_tag_meta:{user_id}:{tid}")
        raw_links = await redis_client.lrange(f"user_tag_links:{tid}", 0, -1)
        
        if not raw_links:
            await query.edit_message_text("⚠️ این برچسب خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
            return
            
        report_text = f"📂 <b>لینک‌های برچسب «{t_name}» (تعداد: {len(raw_links)}):</b>\n\n"
        for idx, l in enumerate(raw_links, 1):
            try:
                ld = json.loads(l)
                report_text += f"{idx}. 📱 <code>{ld['phone']}</code>\n🔗 {ld['link']}\n\n"
            except: pass
            
        if len(report_text) > 4000:
            file_out = io.BytesIO(report_text.encode('utf-8'))
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Tag_{t_name}.txt", caption=f"📂 تمامی لینک‌های مربوط به برچسب: {t_name}")
            await context.bot.send_message(chat_id=user_id, text="بازگشت به لیست برچسب‌ها:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
        else:
            await query.edit_message_text(report_text, disable_web_page_preview=True, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_tags")]]))
        return

    # === بخش مربوط به مدیریت ادمین ===
    if not is_admin(user_id): return
    await query.answer()
    
    if data == "admin_panel":
        context.user_data['admin_zip_action'] = None
        await query.edit_message_text("⚙️ <b>پنل مدیریت سیستم:</b>", reply_markup=get_admin_keyboard(), parse_mode='HTML')
        
    elif data == "admin_set_proxy":
        context.user_data['admin_state'] = 'waiting_for_proxy'
        await query.edit_message_text(
            "🌐 <b>تنظیم پروکسی‌ها:</b>\n\n"
            "لطفاً لیست پروکسی‌های خود را (به صورت متن در همین پیام یا یک فایل `txt.`) ارسال کنید.\n\n"
            "⚠️ <b>فرمت مجاز:</b> `user:pass@host:port` یا `http://user...`", 
            parse_mode='Markdown'
        )

    elif data == "admin_stats":
        acc_keys = await redis_client.keys("account:*")
        link_keys = await redis_client.keys("acc_link:*")
        proxies_json = await redis_client.get("settings:proxies")
        proxy_count = len(json.loads(proxies_json)) if proxies_json else 0
        
        maint = await redis_client.get("settings:maintenance")
        exp = await redis_client.get("settings:expire_time")
        exp = int(exp) if exp else 7200
        exp_str = f"{exp // 86400} روز" if exp >= 86400 else f"{exp // 3600} ساعت"
        status = "غیرفعال 🔴" if maint == "1" else "فعال 🟢"
        
        text = (
            "📊 <b>وضعیت سیستم:</b>\n\n"
            f"👤 <b>تعداد کل اکانت‌ها:</b> <code>{len(acc_keys)}</code>\n"
            f"🔗 <b>لینک‌های فعال:</b> <code>{len(link_keys)}</code>\n"
            f"🌐 <b>تعداد پروکسی‌ها:</b> <code>{proxy_count}</code>\n"
            f"⏳ <b>زمان انقضای لینک‌ها:</b> {exp_str}\n"
            f"🤖 <b>وضعیت ربات:</b> {status}"
        )
        await query.edit_message_text(text, reply_markup=get_admin_keyboard(), parse_mode='HTML')
        
    elif data == "admin_users_report":
        raw_logs = await redis_client.lrange("global_link_logs", 0, -1)
        if not raw_logs:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ گزارشی از ساخت لینک توسط کاربران ثبت نشده است.")
            return
        
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج گزارش کاربران...")
        
        users_data = {}
        for item in raw_logs:
            try:
                entry = json.loads(item)
                uid = entry.get("tg_id")
                if uid not in users_data:
                    users_data[uid] = {
                        "name": entry.get("tg_name", "نامشخص"),
                        "username": entry.get("tg_user", ""),
                        "links": []
                    }
                users_data[uid]["links"].append(entry)
            except Exception: pass
        
        report_text = "📊 <b>گزارش جامع ساخت لینک کاربران:</b>\n\n"
        for uid, udata in users_data.items():
            uname_str = f" (@{udata['username']})" if udata['username'] else ""
            report_text += f"👤 کاربر: {udata['name']}{uname_str}\n🆔 آیدی: <code>{uid}</code>\n🔢 تعداد کل لینک‌ها: {len(udata['links'])}\n------------------------------------\n"
            
        file_out = io.BytesIO(report_text.encode('utf-8'))
        await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Users_Summary_{int(time.time())}.txt", caption="📊 گزارش خلاصه کارکرد کاربران")

    elif data == "admin_expire":
        kb = [
            [InlineKeyboardButton("۱ ساعت ⏱", callback_data="set_exp_3600"), InlineKeyboardButton("۲۴ ساعت 🕐", callback_data="set_exp_86400")],
            [InlineKeyboardButton("۱ هفته 📅", callback_data="set_exp_604800"), InlineKeyboardButton("۱ ماه 📆", callback_data="set_exp_2592000")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_panel")]
        ]
        await query.edit_message_text("⏳ <b>زمان انقضای لینک‌ها را تعیین کنید:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    elif data.startswith("set_exp_"):
        new_time = int(data.split("_")[2])
        await redis_client.set("settings:expire_time", new_time)
        exp_str = f"{new_time // 86400} روز" if new_time >= 86400 else f"{new_time // 3600} ساعت"
        await query.edit_message_text(f"✅ انقضای لینک‌ها با موفقیت به <b>{exp_str}</b> تغییر یافت.", reply_markup=get_admin_keyboard(), parse_mode='HTML')

    elif data == "admin_check_discounts":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال پردازش دیتابیس با سیستم ضدربات (پروکسی). لطفاً منتظر بمانید...")
        asyncio.create_task(process_discounts_and_send_report(context.bot, user_id, acc_keys))

    elif data == "admin_zip_to_link":
        context.user_data['admin_zip_action'] = 'zip_to_link'
        await context.bot.send_message(chat_id=user_id, text="🔗 <b>عملیات استخراج لینک:</b>\nلطفاً فایل ZIP مربوطه را ارسال کنید.", parse_mode='HTML')
        
    elif data == "admin_zip_discount":
        context.user_data['admin_zip_action'] = 'zip_discount_check'
        await context.bot.send_message(chat_id=user_id, text="🔍 <b>عملیات بررسی تخفیف:</b>\nلطفاً فایل ZIP مربوطه را ارسال کنید.", parse_mode='HTML')

    elif data == "admin_export":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        export_text = "لیست شماره‌های ثبت شده در سیستم:\n\n"
        for key in acc_keys: export_text += f"{key.replace('account:', '')}\n"
        
        file_out = io.BytesIO(export_text.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Accounts_{int(time.time())}.txt", caption="📥 فایل دیتابیس (شماره‌ها) دریافت شد.")
        except Exception as e:
            logging.error(f"Error sending export: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل استخراج.")

    elif data == "admin_export_links":
        link_keys = await redis_client.keys("acc_link:*")
        if not link_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ لینکی در دیتابیس موجود نیست.")
            return
            
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج لینک‌ها و شماره‌ها. لطفاً منتظر بمانید...")
        
        export_text = "لیست لینک‌های فعال به همراه شماره:\n\n"
        count = 0
        for l_key in link_keys:
            link_id = l_key.replace("acc_link:", "")
            final_url = f"{WEB_DOMAIN}/acc/{link_id}"
            link_data = await redis_client.get(l_key)
            phone = "نامشخص"
            try:
                data_json = json.loads(link_data)
                origins = data_json.get("origins", [])
                if origins:
                    for item in origins[0].get("localStorage", []):
                        if item.get("name") == "user":
                            user_obj = json.loads(urllib.parse.unquote(item.get("value")))
                            phone = user_obj.get("mobilePhone", "نامشخص")
                            break
            except Exception:
                pass
            export_text += f"📱 شماره: {phone}\n🔗 لینک: {final_url}\n\n"
            count += 1
            
        file_out = io.BytesIO(export_text.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Links_With_Phone_{int(time.time())}.txt", caption=f"✅ استخراج {count} لینک با موفقیت انجام شد.")
        except Exception as e:
            logging.error(f"Error sending links doc: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل لینک‌ها.")

    elif data == "admin_export_tokens":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ دیتابیس سیستم خالی است.")
            return
        await context.bot.send_message(chat_id=user_id, text="⏳ در حال استخراج توکن‌ها...")
        exported_data = {}
        for key in acc_keys:
            phone = key.replace('account:', '')
            tokens = await redis_client.hgetall(key)
            exported_data[phone] = {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", "")
            }
        json_data = json.dumps(exported_data, indent=4, ensure_ascii=False)
        file_out = io.BytesIO(json_data.encode('utf-8'))
        try:
            await context.bot.send_document(chat_id=user_id, document=file_out, filename=f"Tokens_DB_{int(time.time())}.json", caption=f"🔑 فایل توکن‌های استخراج شده ({len(acc_keys)} شماره)")
        except Exception as e:
            logging.error(f"Error sending tokens doc: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ خطا در ارسال فایل توکن‌ها.")

    elif data == "admin_repair_links":
        link_keys = await redis_client.keys("acc_link:*")
        if not link_keys:
            await context.bot.send_message(chat_id=user_id, text="⚠️ هیچ لینکی در سیستم جهت تعمیر وجود ندارد.")
            return
            
        msg = await context.bot.send_message(chat_id=user_id, text="🛠 در حال بررسی و تعمیر لینک‌ها (عملیات دیتابیس داخلی)...")
        repaired_count = 0
        for l_key in link_keys:
            try:
                link_data = await redis_client.get(l_key)
                data_json = json.loads(link_data)
                phone = None
                origins = data_json.get("origins", [])
                if origins:
                    for item in origins[0].get("localStorage", []):
                        if item.get("name") == "user":
                            user_obj = json.loads(urllib.parse.unquote(item.get("value")))
                            phone = user_obj.get("mobilePhone")
                            break
                if phone:
                    acc_data = await redis_client.hgetall(f"account:{phone}")
                    r_tok = acc_data.get("refresh_token")
                    t_ms = acc_data.get("access_token")
                    if r_tok and t_ms:
                        if "cookies" not in data_json:
                            data_json["cookies"] = []
                        data_json["cookies"] = [c for c in data_json["cookies"] if c.get("name") not in ["tokenMS", "token", "refresh_token"]]
                        data_json["cookies"].append({"name": "tokenMS", "value": t_ms, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        data_json["cookies"].append({"name": "token", "value": t_ms, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        data_json["cookies"].append({"name": "refresh_token", "value": r_tok, "domain": ".okala.com", "path": "/", "secure": True, "sameSite": "None"})
                        ttl = await redis_client.ttl(l_key)
                        if ttl > 0:
                            await redis_client.setex(l_key, ttl, json.dumps(data_json, ensure_ascii=False))
                            repaired_count += 1
            except Exception as e:
                logging.error(f"Error repairing link {l_key}: {e}")
        await msg.edit_text(f"✅ عملیات تعمیر پایان یافت.\nتعداد <b>{repaired_count}</b> لینک به صورت خودکار ترمیم شدند.", parse_mode='HTML')

    elif data == "admin_clear":
        kb = [[InlineKeyboardButton("✅ تایید عملیات حذف", callback_data="admin_clear_confirm"), InlineKeyboardButton("❌ انصراف", callback_data="admin_panel")]]
        await query.edit_message_text("⚠️ <b>اخطار:</b> این عملیات تمامی اطلاعات ثبت شده را حذف خواهد کرد.\nآیا تایید می‌کنید؟", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        
    elif data == "admin_clear_confirm":
        acc_keys = await redis_client.keys("account:*")
        if acc_keys: await redis_client.delete(*acc_keys)
        await query.edit_message_text("🗑 عملیات پاکسازی با موفقیت انجام شد.", reply_markup=get_admin_keyboard())
        
    elif data == "admin_toggle":
        current = await redis_client.get("settings:maintenance")
        new_val = "0" if current == "1" else "1"
        await redis_client.set("settings:maintenance", new_val)
        status = "غیرفعال (تعمیرات) 🔴" if new_val == "1" else "فعال 🟢"
        await query.edit_message_text(f"⚙️ <b>تغییر وضعیت سیستم:</b>\nوضعیت کنونی: {status}", reply_markup=get_admin_keyboard(), parse_mode='HTML')

# ==========================================
# سیستم هندل کردن ورودی متنی / فایلی پروکسی
# ==========================================
async def handle_admin_text_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    
    state = context.user_data.get('admin_state')
    
    if state == 'waiting_for_proxy':
        text_content = ""
        msg = await update.message.reply_text("⏳ در حال خواندن پروکسی‌ها...")
        
        try:
            if update.message.document:
                file_name = update.message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    await msg.edit_text("❌ فرمت فایل پروکسی باید `.txt` باشد.")
                    return
                file = await update.message.document.get_file()
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    await file.download_to_drive(temp_file.name)
                    with open(temp_file.name, 'r', encoding='utf-8') as f:
                        text_content = f.read()
            else:
                text_content = update.message.text
                
            proxies = []
            for line in text_content.split('\n'):
                line = line.strip()
                if line:
                    if not line.startswith('http') and not line.startswith('socks'):
                        line = f"http://{line}"
                    proxies.append(line)
                    
            if proxies:
                await redis_client.set("settings:proxies", json.dumps(proxies))
                context.user_data['admin_state'] = None
                await msg.edit_text(f"✅ تعداد <b>{len(proxies)}</b> پروکسی با موفقیت ذخیره و برای بررسی تخفیف‌ها تنظیم شد.", reply_markup=get_admin_keyboard(), parse_mode='HTML')
            else:
                await msg.edit_text("⚠️ متنی حاوی پروکسی یافت نشد. لطفاً مجدداً امتحان کنید.")
                
        except Exception as e:
            logging.error(f"Error reading proxies: {e}")
            await msg.edit_text("❌ خطا در پردازش فایل یا متن پروکسی.")

# ==========================================
# توابع لاگین کاربر 
# ==========================================
def get_user_headers(context: ContextTypes.DEFAULT_TYPE):
    if 'device_id' not in context.user_data:
        context.user_data['device_id'] = str(uuid.uuid4())
        context.user_data['session_id'] = str(uuid.uuid4())
    headers = {
        'accept': 'application/json, text/plain, */*',
        'source': 'okala',
        'ui-version': '2.0',
        'origin': 'https://www.okala.com',
        'User-Agent': random.choice(USER_AGENTS)
    }
    headers['X-User-Unique-Id'] = context.user_data['device_id']
    headers['session-id'] = context.user_data['session_id']
    return headers

async def async_request(method, url, **kwargs):
    loop = asyncio.get_running_loop()
    if method.upper() == 'POST': return await loop.run_in_executor(executor, lambda: requests.post(url, **kwargs))
    return await loop.run_in_executor(executor, lambda: requests.get(url, **kwargs))

async def check_maintenance(update: Update) -> bool:
    maint = await redis_client.get("settings:maintenance")
    user_id = update.effective_user.id if update.effective_user else 0
    if maint == "1" and not is_admin(user_id):
        text = "⛔️ سیستم در حال حاضر موقتاً غیرفعال است."
        if update.message: await update.message.reply_text(text)
        else: await update.callback_query.message.reply_text(text)
        return True
    return False

async def start_login_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    await update.callback_query.answer()
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
    await update.callback_query.edit_message_text("📱 <b>لطفاً شماره موبایل خود را وارد کنید:</b>", reply_markup=kb, parse_mode='HTML')
    return PHONE

async def cancel_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("عملیات لغو شد ❌")
    await show_main_menu(update, context) 
    return ConversationHandler.END

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    
    if response.status_code == 200:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
            [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
        ])
        await update.message.reply_text("✉️ <b>کد تایید ارسال شد.</b>\nلطفاً آن را وارد کنید:", reply_markup=kb, parse_mode='HTML')
        return OTP
    else:
        await update.message.reply_text(f"❌ خطا در ارتباط با سیستم: <code>{response.status_code}</code>", parse_mode='HTML')
        return ConversationHandler.END

async def resend_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone')
    await query.answer("در حال ارسال مجدد کد... ⏳")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
        [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
    ])
    
    if response.status_code == 200:
        await query.edit_message_text(f"✉️ <b>کد تایید مجدداً به {phone} ارسال شد.</b>\nلطفاً کد جدید را وارد کنید:", reply_markup=kb, parse_mode='HTML')
    else:
        await query.edit_message_text(f"❌ خطا در ارسال مجدد: <code>{response.status_code}</code>", reply_markup=kb, parse_mode='HTML')
    return OTP 

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("⏳ در حال پردازش درخواست...")
    
    token_url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    payload = {"mobile_number": phone, "otp_code": otp_code, "grant_type": "customer_grant_type", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "client_name": "customer_client_name", "device_type_code": 7, "scope": "offline_access", "loginDuration": 4815}
    headers = get_user_headers(context)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    response = await async_request('POST', token_url, data=payload, headers=headers)
    
    if response.status_code == 200:
        auth_data = response.json()
        context.user_data['auth_data'] = auth_data 
        if auth_data.get("access_token"):
            await redis_client.hset(f"account:{phone}", mapping={"access_token": auth_data.get("access_token"), "refresh_token": auth_data.get("refresh_token")})
            
        if not auth_data.get("UserInfo", {}).get("HasName", False):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]])
            await msg.edit_text("⚠️ <b>اطلاعات حساب ناقص است.</b>\nلطفاً نام و نام خانوادگی خود را وارد کنید:", reply_markup=kb, parse_mode='HTML')
            return ASK_NAME
        else:
            return await generate_and_send_link(update, context, msg)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 ارسال مجدد کد ورود", callback_data="resend_otp")],
            [InlineKeyboardButton("❌ کنسل عملیات", callback_data="cancel_action")]
        ])
        await msg.edit_text("❌ کد وارد شده اشتباه یا منقضی است.\nلطفاً مجدداً تلاش کنید.", reply_markup=kb)
        return OTP 

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    if not full_name: return ASK_NAME
    parts = full_name.split(maxsplit=1)
    msg = await update.message.reply_text("⏳ در حال ثبت اطلاعات...")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/UpdateCustomer" 
    headers = get_user_headers(context)
    headers["Authorization"] = f"Bearer {context.user_data['auth_data'].get('access_token')}"
    payload = {"birthDate": "", "birthDateEpoch": 700086600, "customerType": 0, "firstName": parts[0], "genderCode": 1, "genderTitle": "مذکر", "lastName": parts[1] if len(parts)>1 else "", "gender": "male"}
    
    await async_request('POST', url, json=payload, headers=headers)
    return await generate_and_send_link(update, context, msg)

async def generate_and_send_link(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    auth_data = context.user_data.get('auth_data')
    phone = context.user_data.get('phone', 'نامشخص')
    injection_json = format_for_injector(auth_data)
    link_id = str(uuid.uuid4())[:12]
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    await redis_client.setex(f"acc_link:{link_id}", expire_time, json.dumps(injection_json, ensure_ascii=False))
    
    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
    
    if 'session_links' not in context.user_data:
        context.user_data['session_links'] = []
    context.user_data['session_links'].append({"phone": phone, "link": final_url})
    
    tg_user = update.effective_user
    log_entry = {
        "tg_id": tg_user.id,
        "tg_name": tg_user.full_name or "نامشخص",
        "tg_user": tg_user.username or "",
        "phone": phone,
        "link": final_url,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    await redis_client.rpush("global_link_logs", json.dumps(log_entry, ensure_ascii=False))
    
    count = len(context.user_data['session_links'])
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت لینک برای یک خط دیگر", callback_data="user_login")],
        [InlineKeyboardButton("🏁 پایان لینک ساختن", callback_data="finish_link_creation")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]
    ])
    
    text = (
        f"✅ <b>ورود به حساب شماره {phone} با موفقیت انجام شد.</b>\n\n"
        f"📥 لینک تولید شد و آماده تحویل است.\n"
        f"📊 تعداد لینک‌های آماده ارسال در این نوبت: <b>{count}</b>\n\n"
        "می‌توانید شماره دیگری اضافه کنید یا دکمه <b>«🏁 پایان لینک ساختن»</b> را بزنید."
    )
    await status_msg.edit_text(text, reply_markup=kb, parse_mode='HTML')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ عملیات متوقف شد.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==========================================
# راه‌اندازی اصلی
# ==========================================
async def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing.")
        return

    await start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', show_main_menu))
    application.add_handler(CommandHandler('admin', admin_command))
    
    application.add_handler(MessageHandler(filters.Document.FileExtension("zip"), handle_zip_upload))
    
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_login_process, pattern="^user_login$"),
            CallbackQueryHandler(ask_tag_name, pattern="^set_tag$"),
            CallbackQueryHandler(ask_search_query, pattern="^search_links$")
        ],
        states={
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, request_otp),
            ],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_and_check_name),
                CallbackQueryHandler(resend_otp_callback, pattern="^resend_otp$"),
            ],
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_name_and_continue),
            ],
            ASK_TAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tag_name),
                CallbackQueryHandler(clear_active_tag_callback, pattern="^clear_active_tag$")
            ],
            ASK_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_query)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(cancel_process_callback, pattern="^cancel_action$")
        ]
    )
    application.add_handler(conv_handler)
    
    application.add_handler(CallbackQueryHandler(core_callback, pattern="^admin_|^set_exp_|^main_menu$|^admin_panel$|^finish_link_creation$|^my_tags$|^show_tag_|^contact_admin$"))
    
    application.add_handler(MessageHandler(filters.TEXT | filters.Document.FileExtension("txt"), handle_admin_text_document))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logging.info("System initialized successfully.")
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
