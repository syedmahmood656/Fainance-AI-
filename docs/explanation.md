# Application Flow Explanation: Ledger-Finance-AI

## Overview
Ledger-Finance-AI is an intelligent expense tracking system that leverages AI to automate the recording of expenses from receipts, CSV files, and natural language queries via a Telegram Bot interface.

### High-Level Architecture
The application follows a modular architecture:
1.  **Interface Layer**: Telegram Bot (via Webhooks) provides the user interface.
2.  **API Layer**: FastAPI handles incoming webhooks from Telegram and manages the application lifecycle.
3.  **Orchestration Layer**: LangGraph manages the logic flow (the "Agent"), routing requests to the correct processing pipeline based on the input type.
4.  **AI Layer**: Specialized AI components handle receipt extraction (OCR/Vision), expense categorization, and report parameter parsing.
5.  **Service Layer**: Business logic for managing expenses, generating reports, and handling file storage.
6.  **Data Layer**: SQLAlchemy-based database for persistent storage of expense records.

---

## Application Flow

### 1. User Interaction (The Trigger)
Users interact with the system through a Telegram Bot. They can:
- **Send a Photo**: The bot captures the image of a receipt.
- **Send a CSV**: The bot captures a file containing bulk expense data.
- **Send a Text Message**: The user asks questions about their spending or requests reports.

### 2. Request Handling (FastAPI $\rightarrow$ Telegram Bot)
- **Webhook**: Telegram sends a POST request to `/telegram/webhook` in the FastAPI app.
- **Authorization**: `app/telegram/bot.py` verifies if the `user_id` is in the `ALLOWED_TELEGRAM_USER_IDS` list.
- **State Preparation**: The bot constructs an `AgentState` object containing the user ID, message text, and any media (image URLs or local paths for CSVs).

### 3. The Agent Loop (LangGraph)
The core logic resides in `app/agent/graph.py`. LangGraph acts as a state machine that routes the request through one of three flows:

#### A. Receipt Flow (Image $\rightarrow$ Record)
1.  **`extract_receipt`**: Uses `ReceiptExtractor` (AI) to extract structured data (Vendor, Amount, Date, etc.) from the image.
2.  **`categorize_expense`**: Uses `ExpenseCategorizer` (AI) to assign a category (e.g., "Travel", "Office Supplies") to the extracted data.
3.  **`save_expense`**: Calls the `ExpenseService` to save the record into the database.
4.  **Response**: The bot sends a confirmation message back to the user.

#### B. CSV Flow (File $\rightarrow$ Bulk Record)
1.  **`process_csv`**: Reads the uploaded CSV file, iterates through the rows, and creates database entries for each expense via `ExpenseService`.
2.  **Response**: The bot informs the user how many records were successfully imported.

#### C. Report Flow (Text $\rightarrow$ Analysis/File)
1.  **`parse_report`**: Uses `ReportParser` (AI) to understand the user's intent (e.g., "August spending" $\rightarrow$ start\_date: 2024-08-01, end\_date: 2024-08-31).
2.  **`execute_report`**: 
    - If it's a general query: Calls `ReportService` to calculate totals or lists.
    - If it's a receipt retrieval: Searches the database for specific receipts and retrieves images from `StorageService`.
    - If Excel is requested: `ReportService` generates an `.xlsx` file.
3.  **Response**: The bot sends the text answer, the Excel file, or the retrieved receipt images.

---

## File & Function Breakdown

### `app/main.py`
- **`startup_event()`**: Initializes the database when the server starts.
- **`telegram_webhook()`**: Entry point for all Telegram messages; passes data to the bot handler.

### `app/telegram/bot.py`
- **`TelegramBot` class**: Wrapper for the Telegram API to send messages, photos, and documents.
- **`handle_telegram_update()`**: The "controller" that parses Telegram updates, checks authorization, prepares the `AgentState`, and invokes the LangGraph `graph`.

### `app/agent/graph.py`
- **`router_node()`**: Determines which flow to take based on the input (image, csv, or text).
- **`create_graph()`**: Defines the nodes (functions) and edges (transitions) of the AI agent.

### `app/ai/receipt_extractor.py`
- **`ReceiptExtractor.extract()`**: Processes images to find financial data.
- **`ExpenseCategorizer.categorize()`**: Uses LLM to map expenses to predefined categories.
- **`ReportParser.parse()`**: Converts natural language (e.g., "last 3 months") into date ranges and filters.

### `app/services/`
- **`expense_service.py`**: Handles the creation and modification of expense records.
- **`report_service.py`**: Logic for aggregating data and generating Excel files.
- **`storage_service.py`**: Manages the saving and retrieval of physical receipt images.

### `app/database/`
- **`models.py`**: Defines the database schema (SQLAlchemy).
- **`repository.py`**: Contains optimized queries for fetching data.

---

## Technologies Used

| Technology | Purpose | Explanation |
| :--- | :--- | :--- |
| **FastAPI** | Web Framework | A high-performance Python framework used to create the API that receives Telegram webhooks. |
| **Telegram Bot API** | Interface | The primary way users interact with the system. |
| **LangGraph** | Agent Orchestration | A library for building stateful, multi-actor applications with LLMs, allowing for complex routing and cycles. |
| **SQLAlchemy** | ORM | Maps Python objects to database tables for structured data storage. |
| **ngrok** | Tunneling | Creates a secure, public URL that tunnels traffic from the internet (Telegram's servers) to the local development environment (PC/Docker). |
| **Docker** | Containerization | Ensures the application runs in a consistent environment regardless of the host OS. |
| **Pandas/Openpyxl** | Data Processing | Used within `ReportService` to aggregate data and write formatted Excel reports. |
