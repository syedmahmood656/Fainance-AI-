# 🚀 How to Install and Run the AI Expense Tracker

This guide will walk you through setting up the AI Expense Tracker on your own computer. We have designed the app to run inside **Docker**, which means you don't need to install PostgreSQL or Python manually on your system—Docker handles everything for you.

---

## 📋 1. What You Need Before Starting

Before you begin, make sure you have these three things installed on your PC:

1.  **Docker Desktop**: [Download here](https://www.docker.com/products/docker-desktop/). (This is the most important part!)
2.  **A Code Editor**: We recommend [VS Code](https://code.visualstudio.com/).
3.  **A Public URL (for Telegram)**: Telegram needs to "talk" to your computer. Since your computer is private, you need a tool like **ngrok** to create a public link to your local app.
    - [Download ngrok here](https://ngrok.com/download).

---

## 🔑 2. Getting Your "Keys" (Credentials)

The app needs a few "keys" to work. You'll need to gather these:

### A. Telegram Bot Token
1. Open Telegram and search for **@BotFather**.
2. Type `/newbot` and follow the instructions to name your bot.
3. BotFather will give you a **Token** (e.g., `123456789:ABCdefGhI...`). Save this.

### B. AI API Key
You need one of the following:
- **OpenAI Key**: Get it from [platform.openai.com](https://platform.openai.com/).
- **Anthropic Key**: Get it from [console.anthropic.com](https://console.anthropic.com/).

### C. Your Telegram User ID
To prevent strangers from using your bot, you need your own ID:
1. Search for **@userinfobot** on Telegram.
2. Send it a message, and it will reply with your **Id** (a long number like `54321678`).

---

## ⚙️ 3. Configuration

1. Open the project folder in your code editor.
2. Look for a file named `.env.example`.
3. **Rename** it to simply `.env`.
4. Open `.env` and fill in your details:

```env
# Your bot token from @BotFather
TELEGRAM_BOT_TOKEN=your_token_here

# Your API key (either OpenAI or Anthropic)
OPENAI_API_KEY=sk-xxxx...
# OR
ANTHROPIC_API_KEY=sk-ant-xxxx...

# The model you want to use (e.g., gpt-4o or claude-3-5-sonnet-20240620)
LLM_MODEL=gpt-4o

# Your Telegram ID (and any other friends' IDs separated by commas)
ALLOWED_TELEGRAM_USER_IDS=54321678,98765432

# Leave this as is for Docker
DATABASE_URL=postgresql://postgres:postgres@db:5432/expense_db
```

---

## 🚀 4. Running the App

### Step 1: Start the App
Open your terminal (Command Prompt or PowerShell) in the project folder and run:
```bash
docker compose up --build -d
```
*This will download the database and the app, and start them in the background.*

### Step 2: Make your App Public (via ngrok)
Telegram cannot see `localhost:8000`. We use ngrok to create a tunnel:
1. Open a new terminal.
2. Run: `ngrok http 8000`
3. ngrok will give you a **Forwarding URL** (e.g., `https://a1b2-c3d4.ngrok-free.app`). **Copy this URL.**

### Step 3: Tell Telegram where to find you (The Webhook)
You need to tell Telegram to send messages to your ngrok URL. Open your browser and visit this link (replace the placeholders):

`https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=<YOUR_NGROK_URL>/telegram/webhook`

# https://api.telegram.org/bot8606009556:AAH4YOVgV_v3mJgQ1l4-q7HUuZ0UKtzAsY8/setWebhook?url=https://barbed-gauze-stopwatch.ngrok-free.dev/telegram/webhook


**Example:**
`https://api.telegram.org/bot12345:ABC/setWebhook?url=https://a1b2.ngrok.app/telegram/webhook`

If you see `{"ok":true,"result":true...}`, you are successful! 🎉

---

## 📱 5. How to Use It

1. Open your bot in Telegram and click **Start**.
2. **Record an Expense**: Take a photo of a receipt and send it to the bot. Wait a few seconds, and it will confirm the amount and category.
3. **Ask a Question**: Type *"How much did I spend this month?"* or *"Show me Travel expenses."*
4. **Get a Report**: Type *"Generate Excel report."*

---

## 🛠 Troubleshooting

- **Bot not responding?** Check if ngrok is still running. If you restart ngrok, your URL changes, and you must repeat **Step 3** (Set Webhook).
- **AI Error?** Double-check that your API key in `.env` is correct and has credits.
- **Unauthorized?** Make sure your Telegram ID is exactly right in the `ALLOWED_TELEGRAM_USER_IDS` section of your `.env` file.
- **To stop the app**: Run `docker compose down`.
