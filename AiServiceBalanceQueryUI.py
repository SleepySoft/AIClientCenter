import sys
import asyncio
import threading
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                             QLabel, QDoubleSpinBox, QComboBox, QHeaderView, QMessageBox)
from PyQt5.QtCore import Qt, pyqtSignal, QObject

try:
    from AiServiceBalanceQuery import BalanceQueryService
except ImportError as e:
    print(str(e))
    from .AiServiceBalanceQuery import BalanceQueryService


class QueryWorker(QObject):
    """
    Worker to handle asynchronous API calls without freezing the UI.
    """
    finished = pyqtSignal(list)  # List of dict results
    progress = pyqtSignal(str)

    def __init__(self, platform, keys):
        super().__init__()
        self.platform = platform
        self.keys = keys

    def run(self):
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        results = loop.run_until_complete(self.async_batch_query())
        loop.close()
        self.finished.emit(results)

    async def async_batch_query(self):
        service = BalanceQueryService()
        final_results = []

        for key in self.keys:
            key = key.strip()
            if not key: continue

            self.progress.emit(f"Querying: {key[:15]}...")
            try:
                if self.platform == "SiliconFlow":
                    res = await service.query_siliconflow(key)
                elif self.platform == "OpenAI":
                    res = await service.query_openai(key)
                else:  # DeepSeek
                    res = await service.query_deepseek(key)

                # Attach the key to the result for identification
                res['_api_key'] = key
                final_results.append(res)
            except Exception as e:
                final_results.append({"success": False, "error": str(e), "_api_key": key})

        await service.close()
        return final_results


class AI_Balance_Checker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()
        self.all_results = []  # Cache for filtering

    def init_ui(self):
        self.setWindowTitle("AI Service Balance Checker")
        self.setMinimumSize(1000, 700)

        # Main Layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # --- Left Panel: Input ---
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Step 1: Paste API Keys (one per line)"))
        self.key_input = QTextEdit()
        self.key_input.setPlaceholderText("sk-...\nsk-...")
        left_layout.addWidget(self.key_input)

        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["SiliconFlow", "OpenAI", "DeepSeek"])
        left_layout.addWidget(QLabel("Select Platform:"))
        left_layout.addWidget(self.platform_combo)

        self.btn_query = QPushButton("Start Query")
        self.btn_query.setFixedHeight(40)
        self.btn_query.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold;")
        self.btn_query.clicked.connect(self.start_query)
        left_layout.addWidget(self.btn_query)

        main_layout.addLayout(left_layout, 1)

        # --- Middle Panel: Results Table ---
        mid_layout = QVBoxLayout()
        mid_layout.addWidget(QLabel("Step 2: Balance Details"))
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["API Key (Suffix)", "Balance", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        mid_layout.addWidget(self.table)

        self.status_label = QLabel("Ready")
        mid_layout.addWidget(self.status_label)

        main_layout.addLayout(mid_layout, 2)

        # --- Right Panel: Filter and Export ---
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Step 3: Filter & Extract"))

        filter_box = QHBoxLayout()
        filter_box.addWidget(QLabel("Balance >"))
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0, 999999)
        self.threshold_spin.setValue(0.1)
        filter_box.addWidget(self.threshold_spin)
        right_layout.addLayout(filter_box)

        self.btn_filter = QPushButton("Extract Valid Keys")
        self.btn_filter.clicked.connect(self.filter_keys)
        right_layout.addWidget(self.btn_filter)

        self.extracted_output = QTextEdit()
        self.extracted_output.setReadOnly(True)
        self.extracted_output.setPlaceholderText("Qualified keys will appear here...")
        right_layout.addWidget(self.extracted_output)

        self.btn_copy = QPushButton("Copy All to Clipboard")
        self.btn_copy.clicked.connect(lambda: self.extracted_output.selectAll() or self.extracted_output.copy())
        right_layout.addWidget(self.btn_copy)

        main_layout.addLayout(right_layout, 1)

    def start_query(self):
        keys = self.key_input.toPlainText().strip().split('\n')
        keys = [k.strip() for k in keys if k.strip()]

        if not keys:
            QMessageBox.warning(self, "Error", "Please input at least one API Key")
            return

        self.btn_query.setEnabled(False)
        self.table.setRowCount(0)
        self.all_results = []

        # Start Worker Thread
        self.thread = threading.Thread(target=self.run_worker, args=(self.platform_combo.currentText(), keys))
        self.thread.start()

    def run_worker(self, platform, keys):
        # Create worker and connect signals
        self.worker = QueryWorker(platform, keys)
        self.worker.progress.connect(self.update_status)
        self.worker.finished.connect(self.on_query_finished)
        self.worker.run()

    def update_status(self, msg):
        self.status_label.setText(msg)

    def on_query_finished(self, results):
        self.all_results = results
        self.btn_query.setEnabled(True)
        self.status_label.setText(f"Done. Queried {len(results)} keys.")
        self.refresh_table(results)

    def refresh_table(self, results):
        self.table.setRowCount(len(results))
        for row, res in enumerate(results):
            # Key Display (masking prefix)
            key_raw = res.get('_api_key', '')
            display_key = f"...{key_raw[-8:]}" if len(key_raw) > 8 else key_raw

            balance = "N/A"
            status = "Success"

            if res.get('success'):
                data = res.get('data', {})
                # Adapt based on platform data structure
                balance = data.get('total_balance_usd') or data.get('remaining_balance_usd') or data.get(
                    'total_balance') or 0
            else:
                status = res.get('error', 'Unknown Error')
                balance = -1

            self.table.setItem(row, 0, QTableWidgetItem(display_key))
            self.table.setItem(row, 1, QTableWidgetItem(str(balance)))
            self.table.setItem(row, 2, QTableWidgetItem(status))

    def filter_keys(self):
        threshold = self.threshold_spin.value()
        valid_keys = []

        for res in self.all_results:
            if not res.get('success'): continue

            data = res.get('data', {})
            # Unified balance check
            val = data.get('total_balance_usd') or data.get('remaining_balance_usd') or data.get('total_balance') or 0

            if float(val) >= threshold:
                valid_keys.append(res.get('_api_key'))

        self.extracted_output.setPlainText("\n".join(valid_keys))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AI_Balance_Checker()
    window.show()
    sys.exit(app.exec_())
