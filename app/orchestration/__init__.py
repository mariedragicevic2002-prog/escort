"""
app.orchestration — layered orchestration facade.

Layer order:
  WebhookController → InboundMiddlewarePipeline → ConversationEngine
      → ResponseComposer → OutboundDispatcher
"""
