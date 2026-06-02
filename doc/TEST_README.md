# Test Instructions

This project includes automated tests for the AI interfaces of the `agent_controller` and `agent_pilot` components.

## Running Tests

Tests are written using the standard Python `unittest` framework. They use mocking so they run locally without requiring network requests to the Gemini API or needing an API key.

To run all tests, navigate to the project root directory and execute:

```bash
python -m unittest discover -s test
```

## Coverage

The tests verify:
1. **Controller**: The infinite control loop can successfully retrieve state, construct a prompt, call the AI model (`GenerativeModel.generate_content`), and dispatch the radio command correctly.
2. **Pilot**: The `/radio_RX` endpoint successfully parses incoming radio text, calls the AI model to extract JSON parameters, matches the target drone name to its UUID, and prepares the backend API updates.

Both tests completely mock `google.generativeai` and `httpx` to guarantee stable test execution.
