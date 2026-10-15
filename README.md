# Employee Attendance & Analytics API

A FastAPI-based employee attendance system with MongoDB backend, built for the HROne engineering assignment.

## Features

- Employee management (create, list)
- Attendance tracking (punch-in, punch-out)
- Manual attendance regularization with audit trail
- Analytics endpoints:
  - Employee monthly summary
  - Department summary
  - Late-comers leaderboard
  - Department trend analysis
- Query explain endpoint for performance verification

## Tech Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+
- Pydantic 2.6+

## Setup

### Prerequisites

- Python 3.11 or higher
- MongoDB 6.0+ (local Docker or Atlas)

### Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your MongoDB connection details
```

### MongoDB Setup

#### Local Docker
```bash
docker run -p 27017:27017 mongo:7
```

#### MongoDB Atlas
- Create a free M0 cluster
- Add your IP to the whitelist
- Copy the connection string to `.env`

### Load Sample Data

```bash
python sample_seed.py
```

## Running the Application

```bash
uvicorn app.main:app --port 8000
```

The API will be available at `http://localhost:8000`

## API Documentation

Once running, visit:
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI spec: `http://localhost:8000/openapi.json`

## Health Check

```bash
curl http://localhost:8000/health
```

## Key Implementation Details

- **Indexes**: Created at startup for performance on large datasets
- **Concurrency**: Race conditions handled via database unique indexes and atomic updates
- **Time Zone**: All instants stored as UTC datetimes, converted to epoch milliseconds at API boundary
- **Business Rules**: Fully implemented R1-R10 from the specification
- **Analytics**: Implemented as MongoDB aggregation pipelines for performance

## Testing

The implementation includes:
- Proper error handling (404, 409, 422 status codes)
- Validation of all input parameters
- Support for concurrent requests
- Performance optimizations for ~100k record datasets

## Files

- `app/main.py` - Complete API implementation
- `requirements.txt` - Python dependencies
- `REVIEW.md` - Defects found and fixed in starter code
- `DECISIONS.md` - Implementation decisions and trade-offs
- `DATA_MODEL.md` - MongoDB data model specification
- `openapi.yaml` - Complete API contract
- `sample_data/` - Sample MongoDB documents
- `sample_seed.py` - Script to load sample data

## Submission

This project is ready for submission as per the assignment requirements:
- All endpoints implemented per openapi.yaml
- No secrets or .env files committed
- REVIEW.md and DECISIONS.md completed
- Indexes created at startup
- Environment variables for MongoDB configuration
