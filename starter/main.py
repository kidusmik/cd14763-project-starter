"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3

# Patch Python 3.14 shutdown_default_executor bug with nest_asyncio
async def _safe_shutdown_default_executor(self, timeout=None):
    pass

asyncio.BaseEventLoop.shutdown_default_executor = _safe_shutdown_default_executor

from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create the single BedrockAgentCoreApp instance at module level
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── 2 — Configuration ─────────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-quplr1vr5l.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "JZKRNN2VZB"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-PJ9jfsE0u9"

# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

# BedrockModel instance
model = BedrockModel(model_id=model_id)

# AgentCore MemoryClient
memory_client = MemoryClient(region_name=REGION)

# Bedrock Agent Runtime client for Knowledge Base retrieval
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    namespaces = {}
    for strategy in strategies:
        strat_type = strategy.get("type")
        template = None
        # Support both modern namespaceTemplates and legacy namespaces
        if "namespaceTemplates" in strategy and strategy["namespaceTemplates"]:
            template = strategy["namespaceTemplates"][0]
        elif "namespaces" in strategy and strategy["namespaces"]:
            template = strategy["namespaces"][0]

        if strat_type and template:
            namespaces[strat_type] = template
    return namespaces


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = getattr(event.agent, "messages", [])
        if not messages:
            return

        last_message = messages[-1]
        role = last_message.get("role") if isinstance(last_message, dict) else getattr(last_message, "role", None)
        if role != "user":
            return

        content = last_message.get("content") if isinstance(last_message, dict) else getattr(last_message, "content", None)

        user_query = ""
        is_tool_result = False

        if isinstance(content, str):
            user_query = content
        elif isinstance(content, list) and len(content) > 0:
            first_block = content[0]
            if isinstance(first_block, dict):
                if "toolResult" in first_block:
                    is_tool_result = True
                else:
                    user_query = first_block.get("text", "")
            elif isinstance(first_block, str):
                user_query = first_block

        if is_tool_result or not user_query:
            return

        # Query all strategy namespaces
        all_memories = []
        for strategy_type, template in self.namespaces.items():
            formatted_ns = template.format(actorId=self.actor_id) if "{actorId}" in template else template
            try:
                records = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=formatted_ns,
                    query=user_query,
                    top_k=5,
                )
                for record in records:
                    text = ""
                    if isinstance(record, dict):
                        c = record.get("content", {})
                        if isinstance(c, dict):
                            text = c.get("text", "")
                        elif isinstance(c, str):
                            text = c
                        elif "text" in record:
                            text = record["text"]
                    elif isinstance(record, str):
                        text = record

                    if text and text.strip():
                        all_memories.append(f"- [{strategy_type}] {text.strip()}")
            except Exception as e:
                logger.warning(f"Failed to retrieve memory for {strategy_type}: {e}")

        if all_memories:
            memories_block = "\n".join(all_memories)
            augmented_text = f"Customer Context:\n{memories_block}\n\n{user_query}"
            if isinstance(content, str):
                if isinstance(last_message, dict):
                    last_message["content"] = augmented_text
                else:
                    last_message.content = augmented_text
            elif isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    content[0]["text"] = augmented_text
                else:
                    content[0] = {"text": augmented_text}

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = getattr(event.agent, "messages", [])
        if not messages:
            return

        customer_query = None
        agent_response = None

        for msg in reversed(messages):
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)

            if role == "assistant" and not agent_response:
                if isinstance(content, str):
                    agent_response = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and "text" in b:
                            agent_response = b["text"]
                            break
            elif role == "user" and not customer_query:
                if isinstance(content, str):
                    customer_query = content
                elif isinstance(content, list):
                    is_tool = any(isinstance(b, dict) and "toolResult" in b for b in content)
                    if not is_tool:
                        for b in content:
                            if isinstance(b, dict) and "text" in b:
                                text_val = b["text"]
                                if "Customer Context:\n" in text_val and "\n\n" in text_val:
                                    customer_query = text_val.split("\n\n", 1)[1]
                                else:
                                    customer_query = text_val
                                break

            if customer_query and agent_response:
                break

        if customer_query and agent_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
                )
            except Exception as e:
                logger.warning(f"Failed to create memory event: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."

    try:
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = response.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = []
        for r in results:
            content = r.get("content", {})
            text = content.get("text", "")
            if text:
                chunks.append(text.strip())

        return "\n---\n".join(chunks) if chunks else "No relevant information found in the knowledge base."
    except Exception as e:
        logger.error(f"Error searching knowledge base: {e}")
        return f"Error searching knowledge base: {e}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

tier_rate = tier_rates.get(tier, 0.0)
earn_rate = earn_rates.get(product_category, 1)

# Points redemption: 100 points = $1. Floor to nearest 500 points.
# Cap points redemption at 50% of the total order value.
max_points_for_half_order = int((order_total * 0.5) * 100)
eligible_points = (loyalty_points // 500) * 500
points_redeemed = min(eligible_points, (max_points_for_half_order // 500) * 500)

points_discount = points_redeemed / 100.0
subtotal_after_points = max(0.0, order_total - points_discount)

tier_discount = round(subtotal_after_points * tier_rate, 2)
final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_discount + tier_discount, 2)
remaining_points = loyalty_points - points_redeemed
points_earned = int(final_total * earn_rate)

result = {{
    "points_redeemed": points_redeemed,
    "tier_discount_pct": int(tier_rate * 100),
    "final_total": final_total,
    "remaining_points": remaining_points,
    "points_discount": points_discount,
    "tier_discount": tier_discount,
    "total_savings": total_savings,
    "points_earned": points_earned
}}
print(json.dumps(result))
"""

    try:
        session = code_session(REGION)
        if hasattr(session, "__enter__"):
            with session as client:
                response = client.invoke(
                    "executeCode",
                    {"code": code, "language": "python", "clearContext": True},
                )
        else:
            response = session.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )

        for event in response:
            if isinstance(event, dict):
                if "result" in event:
                    return json.dumps(event["result"]) if not isinstance(event["result"], str) else event["result"]
                if "stdout" in event:
                    return event["stdout"].strip()
            return json.dumps(event)

        return json.dumps({"status": "completed"})

    except Exception as e:
        logger.warning(f"Code Interpreter execution failed, running fallback: {e}")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)
        return json.dumps({
            "points_redeemed": 0,
            "tier_discount_pct": int(tier_rate * 100),
            "final_total": final_total,
            "remaining_points": loyalty_points,
            "fallback": True,
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        customer_id = payload.get("customer_id", "CUST-123")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(
            actor_id=customer_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        browser_tool = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            browser_tool.browser,
        ]

        system_prompt = (
            "You are an intelligent customer support assistant for an e-commerce platform. "
            "You can track orders, initiate and check refunds, answer product and policy questions "
            "using your knowledge base, calculate loyalty discounts using code execution, and browse the web. "
            "Use your tools whenever you need to fetch real data or compute exact totals."
        )

        # Connect to Gateway via MCPClient and load external tools
        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=system_prompt,
            )

            response = agent(user_input)

            if hasattr(response, "message") and isinstance(response.message, dict):
                content = response.message.get("content", [])
                if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
                    return content[0].get("text", str(response))
                return str(response.message.get("content", response))
            elif hasattr(response, "text"):
                return response.text
            return str(response)

    except Exception as e:
        logger.error(f"Execution error in invoke: {e}", exc_info=True)
        return f"An error occurred: {str(e)}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()