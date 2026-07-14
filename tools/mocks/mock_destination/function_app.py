"""Mock alert destination (TEST ONLY). A private HTTP-triggered Function that
logs whatever alert it receives so you can watch the loop end-to-end in Log
Analytics. Swap for real Torq by editing config/destinations.yaml - no code
change to the engine."""
import logging
import azure.functions as func

app = func.FunctionApp()


@app.function_name(name="mockdest")
@app.route(route="alert", auth_level=func.AuthLevel.FUNCTION)
def mockdest(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_body().decode("utf-8")
    logging.warning("MOCK_DESTINATION received alert: %s", body)
    return func.HttpResponse("ok", status_code=200)
