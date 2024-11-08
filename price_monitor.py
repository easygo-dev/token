import os
import json
import time
import asyncio
import logging
import logging.handlers
from datetime import datetime
from typing import Dict, Any

from telegram import Bot
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

class PriceMonitor:
    def __init__(self):
        self.config = {
            'token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
            'price_threshold': float(os.getenv('PRICE_CHANGE_THRESHOLD', 5)),
            'mcap_threshold': float(os.getenv('MCAP_CHANGE_THRESHOLD', 5)),
            'check_interval': int(os.getenv('CHECK_INTERVAL', 300)),
            'token_address': '0x96db3e22fdac25c0dff1cab92ae41a697406db7d',
            'network': 'SHAPE_MAINNET',
            'data_file': 'data.json',
        }

        self.setup_logging()
        self.bot = Bot(token=self.config['token'])
        self.browser = None
        self.context = None
        self.page = None
        
        self.logger.info("Price monitor initialized with config: %s", 
                        {k: v for k, v in self.config.items() if k != 'token'})

    def setup_logging(self):
        self.logger = logging.getLogger('PriceMonitor')
        self.logger.setLevel(logging.INFO)
        
        os.makedirs('logs', exist_ok=True)
        
        handler = logging.handlers.TimedRotatingFileHandler(
            'logs/price_monitor.log',
            when='midnight',
            interval=1,
            backupCount=14,
            encoding='utf-8'
        )
        
        console_handler = logging.StreamHandler()
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(handler)
        self.logger.addHandler(console_handler)

    async def setup_browser(self):
        """Инициализация браузера"""
        if not self.browser:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox']
            )
            self.context = await self.browser.new_context()
            self.page = await self.context.new_page()
            self.logger.info("Browser initialized")

    def load_data(self) -> Dict[str, Any]:
        try:
            with open(self.config['data_file'], 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.info("No previous data found, starting fresh")
            return {
                'price': 0,
                'market_cap': 0,
                'last_notification_price': 0,
                'last_notification_market_cap': 0
            }

    def save_data(self, data: Dict[str, Any]):
        try:
            with open(self.config['data_file'], 'w') as f:
                json.dump(data, f, indent=2)
            self.logger.info("Data saved successfully")
        except Exception as e:
            self.logger.error("Error saving data: %s", e)

    async def fetch_token_data(self) -> Dict[str, Any]:
        """Получение данных о токене через GraphQL"""
        try:
            # Ждем пока Cloudflare пропустит
            response = await self.page.evaluate("""
                async () => {
                    const response = await fetch("https://zapper.xyz/z/graphql", {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'apollographql-client-name': 'web-relay'
                        },
                        body: JSON.stringify({
                            query: `
                                query TokenPrice($address: Address!, $network: Network!) {
                                    fungibleToken(address: $address, network: $network) {
                                        symbol
                                        name
                                        onchainMarketData {
                                            price
                                            marketCap
                                        }
                                    }
                                }
                            `,
                            variables: {
                                "address": "0x96db3e22fdac25c0dff1cab92ae41a697406db7d",
                                "network": "SHAPE_MAINNET"
                            }
                        })
                    });
                    return response.json();
                }
            """)
            
            if 'errors' in response:
                raise Exception(f"GraphQL errors: {response['errors']}")
                
            return response['data']['fungibleToken']
            
        except Exception as e:
            self.logger.error("Error fetching token data: %s", e)
            # Сохраняем скриншот для отладки
            try:
                await self.page.screenshot(path='error_screenshot.png')
                self.logger.info("Error screenshot saved")
                self.logger.error("Page content: %s", await self.page.content())
            except:
                pass
            raise

    async def send_notification(self, message: str):
        try:
            await self.bot.send_message(
                chat_id=self.config['chat_id'],
                text=message,
                parse_mode='HTML'
            )
            self.logger.info("Notification sent: %s", message)
        except Exception as e:
            self.logger.error("Error sending notification: %s", e)

    async def check_price_changes(self):
        try:
            if not self.page:
                await self.setup_browser()
            
            # Загружаем страницу и ждем пока Cloudflare пропустит
            url = f"https://zapper.xyz/token/shape/{self.config['token_address']}/O/details"
            await self.page.goto(url, wait_until='networkidle')
            await self.page.wait_for_load_state('networkidle')
            
            # Небольшая пауза для полной загрузки
            await asyncio.sleep(5)
            
            previous_data = self.load_data()
            token_data = await self.fetch_token_data()
            
            current_price = token_data['onchainMarketData']['price']
            current_market_cap = token_data['onchainMarketData']['marketCap']

            price_change = self.calculate_percentage_change(
                current_price,
                previous_data['last_notification_price'] or current_price
            )
            
            mcap_change = self.calculate_percentage_change(
                current_market_cap,
                previous_data['last_notification_market_cap'] or current_market_cap
            )

            self.logger.info(
                "Current check - Symbol: %s, Price: %s, Market Cap: %s, "
                "Price Change: %s%%, Market Cap Change: %s%%",
                token_data['symbol'], current_price, current_market_cap,
                price_change, mcap_change
            )

            should_notify = False
            notification_message = f"<b>{token_data['name']} ({token_data['symbol']}) Update</b>\n\n"

            if abs(price_change) >= self.config['price_threshold']:
                should_notify = True
                notification_message += f"💰 Price: ${current_price:.8f}\n"
                notification_message += f"📈 Price Change: {price_change:.2f}%\n"

            if abs(mcap_change) >= self.config['mcap_threshold']:
                should_notify = True
                notification_message += f"🏦 Market Cap: ${current_market_cap:.2f}\n"
                notification_message += f"📊 Market Cap Change: {mcap_change:.2f}%\n"

            if should_notify:
                await self.send_notification(notification_message)
                self.save_data({
                    'price': current_price,
                    'market_cap': current_market_cap,
                    'last_notification_price': current_price,
                    'last_notification_market_cap': current_market_cap,
                    'last_update': datetime.now().isoformat()
                })
            else:
                self.save_data({
                    **previous_data,
                    'price': current_price,
                    'market_cap': current_market_cap,
                    'last_update': datetime.now().isoformat()
                })

        except Exception as e:
            self.logger.error("Error in check cycle: %s", e)
            # Пересоздаем браузер в случае ошибки
            try:
                await self.cleanup()
            except:
                pass
            raise

    def calculate_percentage_change(self, current: float, previous: float) -> float:
        return 0 if previous == 0 else ((current - previous) / previous) * 100

    async def cleanup(self):
        """Очистка ресурсов браузера"""
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        self.page = None
        self.context = None
        self.browser = None

    async def run(self):
        self.logger.info("Starting price monitor...")
        
        while True:
            try:
                await self.check_price_changes()
                await asyncio.sleep(self.config['check_interval'])
            except Exception as e:
                self.logger.error("Error in main loop: %s", e)
                await asyncio.sleep(60)

    async def __aenter__(self):
        await self.setup_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

async def main():
    async with PriceMonitor() as monitor:
        await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())
