# UFC Picks Backend

FastAPI-based backend for the UFC Picks application. Manages user authentication, event data, fight predictions, and leaderboards.

## Prerequisites

- Python 3.11+
- MongoDB Atlas account
- Google OAuth credentials

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create `.env` file

```env
MONGODB_URI=mongodb+srv://...
MONGODB_DB_NAME=ufc_picks
JWT_SECRET=your-secret-key-here
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
CORS_ORIGINS=http://localhost:3000
APP_ENV=development
DEBUG=true
```

### 3. Run locally

```bash
uvicorn app.main:app --reload
```

API will be available at `http://localhost:8000`
- Docs: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Project Structure

```
app/
├── controllers/       # API endpoints
├── services/         # Business logic
├── repositories/     # Database access
├── models/           # MongoDB models
├── schemas/          # Request/response schemas
├── core/             # Config and security
└── utils/            # Helper functions
```

## Testing

```bash
pytest                    # Run all tests
pytest tests/unit/        # Unit tests
pytest tests/integration/ # Integration tests
```

## Deployment

Deployed on [Render](https://render.com) using the `render.yaml` blueprint. See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed instructions.

## API Endpoints

- `POST /auth/register` - Register new user
- `POST /auth/login` - User login
- `GET /events` - Get upcoming events
- `POST /picks` - Submit picks for fights
- `GET /leaderboard` - Get leaderboard rankings
