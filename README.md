NBNL Backend API settings
# NBNL Backend

A FastAPI backend application.

## Prerequisites

Before setting up the project, ensure you have the following installed on your machine:

*   **Git**: To clone the repository.
*   **[uv](https://github.com/astral-sh/uv)**: This project uses `uv` for extremely fast Python package and environment management. 
    *   *Mac/Linux*: `curl -LsSf https://astral.sh/uv/install.sh | sh`
    *   *Windows*: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

## Getting Started

Follow these steps to set up the project locally and run the development server.

### 1. Clone the Repository
```bash
git clone <your-repository-url>
cd <your-repository-name>
```

### 2. Set Up the Virtual Environment and Dependencies
This project uses a `pyproject.toml` and a `uv.lock` file to manage dependencies. You do not need to manually create a virtual environment. Run the following command in the root directory:

```bash
uv sync
```

#### What this does:

- Automatically creates a .venv (virtual environment) folder.

- Installs the exact versions of all dependencies (like FastAPI and Pydantic).

- Ensures you are using the correct Python version specified in .python-version.

### 3. Activate the Virtual Environment
Activate the environment so your terminal uses the installed packages. Run the command based on your operating system:

Windows (Command Prompt):
```
cmd
.venv\Scripts\activate.bat
```
Windows (PowerShell):
```
powershell
.venv\Scripts\Activate.ps1
```
Mac/Linux:
```
bash
source .venv/bin/activate
```
(Note: If you prefer not to activate the environment, you can prefix subsequent commands with ```uv``` run, e.g., ```uv run uvicorn main:app --reload```).

### 4. Run the Application
Start the FastAPI development server with hot-reloading enabled:
```
bash
uvicorn main:app --reload
```
"C:\Users\User\OneDrive\Desktop\NBNL_backend"