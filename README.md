# Django Route API & Performance Tracker

A Django web application featuring custom middleware for real-time HTTP request execution timing and logging.

## Features

- **Request Timing Middleware**: Custom middleware that measures exact execution time, response status codes, payload sizes, and query paths using Python's native `logging` module.
- **REST API Endpoints**: Sample endpoints with query parameter parsing (e.g., location/route queries).

## Prerequisites

Ensure you have the following installed locally:
- **Python**: 3.10 or higher
- **Git**

---

## Quick Start

Follow these steps to set up and run the project locally from scratch.

### 1. Clone the Repository

```bash
git clone https://github.com/Hekewa/django-geo-example.git
cd django-geo-example
```

### 2. Create & Activate a Virtual Environment

* **macOS / Linux:**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

* **Windows (PowerShell):**
  ```powershell
  python -m venv venv
  .\venv\Scripts\Activate.ps1
  ```

* **Windows (Command Prompt):**
  ```cmd
  venv\Scripts\activate.bat
  ```

### 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Start the Development Server

```bash
python manage.py runserver
```

The application will be accessible at `http://127.0.0.1:8000/`.

---

## Example Usage

Test the route API endpoint via `curl` or your browser:

```bash
curl "http://127.0.0.1:8000/api/route/?start=Los%20Angeles&end=New%20York"
```
[http://127.0.0.1:8000/api/route/?start=Los%20Angeles&end=New%20York](http://127.0.0.1:8000/api/route/?start=Los%20Angeles&end=New%20York)