import os
import json
import time
import asyncio
import logging
import logging.handlers
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal

from web3 import Web3
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()

# ABI для ERC20 токена
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]

class PriceMonitor:
    def __init__(self):
        self.config = {
            'token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
            'price_threshold': float(os.getenv('PRICE_CHANGE_THRESHOLD', 5)),
            'mcap_threshold': float(os.getenv('MCAP_CHANGE_THRESHOLD', 5)),
            'check_interval': int(os.getenv('CHECK_INTERVAL', 60)),
            'token_address': '0x96db3e22fdac25c0dff1cab92ae41a697406db7d',
            'rpc_url': 'https://rpc.ankr.com/shapella',  # RPC для Shape сети
            'data_file': 'data.json',
        }
        
        self.setup_logging()
        self.setup_web3()
        self.bot = Bot(token=self.config['token'])
        
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

    def setup_web3(self):
        """Настройка Web3 подключения"""
        self.w3 = Web3(Web3.HTTPProvider(self.config['rpc_url']))
        self.token_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config['token_address']),
            abi=ERC20_ABI
        )

    def load_data(self) -> Dict[str, Any]:
        try:
            with open(self.config['data_file'], 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            self.logger.info("No previous data found, starting fresh")
            return {
                'total_supply': 0,
                'circulating_supply': 0,
                'last_total_supply': 0,
                'last_circulating_supply': 0,
                'last_update': None
            }

    def save_data(self, data: Dict[str, Any]):
        try:
            with open(self.config['data_file'], 'w') as f:
                json.dump(data, f, indent=2)
            self.logger.info("Data saved successfully")
        except Exception as e:
            self.logger.error("Error saving data: %s", e)

    def get_token_data(self) -> Tuple[Decimal, Decimal]:
        """Получение данных о токене напрямую из контракта"""
        try:
            decimals = self.token_contract.functions.decimals().call()
            total_supply = Decimal(self.token_contract.functions.totalSupply().call())
            total_supply = total_supply / Decimal(10 ** decimals)
            
            # Получаем балансы основных адресов (например, burn addresses)
            burn_addresses = [
                "0x000000000000000000000000000000000000dead",
                "0x0000000000000000000000000000000000000000"
            ]
            
            locked_supply = Decimal(0)
            for addr in burn_addresses:
                balance = self.token_contract.functions.balanceOf(
                    Web3.to_checksum_address(addr)
                ).call()
                locked_supply += Decimal(balance)
            
            locked_supply = locked_supply / Decimal(10 ** decimals)
            circulating_supply = total_supply - locked_supply
            
            return total_supply, circulating_supply
            
        except Exception as e:
            self.logger.error("Error fetching token data: %s", e)
            raise

    def calculate_percentage_change(self, current: Decimal, previous: Decimal) -> float:
        if previous == 0:
            return 0
        return float((current - previous) / previous * 100)

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

    async def check_supply_changes(self):
        try:
            previous_data = self.load_data()
            
            total_supply, circulating_supply = self.get_token_data()
            
            total_supply_change = self.calculate_percentage_change(
                total_supply,
                Decimal(previous_data['last_total_supply'] or str(total_supply))
            )
            
            circulating_change = self.calculate_percentage_change(
                circulating_supply,
                Decimal(previous_data['last_circulating_supply'] or str(circulating_supply))
            )

            self.logger.info(
                "Current check - Total Supply: %s, Circulating Supply: %s, "
                "Total Change: %s%%, Circulating Change: %s%%",
                total_supply, circulating_supply,
                total_supply_change, circulating_change
            )

            should_notify = False
            notification_message = f"<b>Circle (O) Supply Update</b>\n\n"

            if abs(total_supply_change) >= self.config['price_threshold']:
                should_notify = True
                direction = "📈" if total_supply_change > 0 else "📉"
                notification_message += f"💰 Total Supply: {total_supply:,.2f}\n"
                notification_message += f"{direction} Supply Change: {total_supply_change:.2f}%\n"

            if abs(circulating_change) >= self.config['mcap_threshold']:
                should_notify = True
                direction = "📈" if circulating_change > 0 else "📉"
                notification_message += f"🏦 Circulating Supply: {circulating_supply:,.2f}\n"
                notification_message += f"{direction} Circulating Change: {circulating_change:.2f}%\n"

            if should_notify:
                await self.send_notification(notification_message)
                self.save_data({
                    'total_supply': str(total_supply),
                    'circulating_supply': str(circulating_supply),
                    'last_total_supply': str(total_supply),
                    'last_circulating_supply': str(circulating_supply),
                    'last_update': datetime.now().isoformat()
                })
            else:
                self.save_data({
                    **previous_data,
                    'total_supply': str(total_supply),
                    'circulating_supply': str(circulating_supply),
                    'last_update': datetime.now().isoformat()
                })

        except Exception as e:
            self.logger.error("Error in check cycle: %s", e)
            raise

    async def run(self):
        self.logger.info("Starting supply monitor...")
        
        while True:
            try:
                await self.check_supply_changes()
                await asyncio.sleep(self.config['check_interval'])
            except Exception as e:
                self.logger.error("Error in main loop: %s", e)
                await asyncio.sleep(60)

async def main():
    monitor = PriceMonitor()
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())
