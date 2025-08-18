#%%writefile agent.py
import mlflow

from typing import Any, Optional, TypedDict, List, Dict
from databricks_langchain import ChatDatabricks, UCFunctionToolkit
from langchain_core.language_models import LanguageModelLike
from langchain_core.runnables import RunnableConfig, RunnableLambda  
from langchain_core.tools import BaseTool, tool 
from langgraph.graph import END, StateGraph
from langgraph.graph.graph import CompiledGraph  
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolNode  
from mlflow.langchain.chat_agent_langgraph import ChatAgentState, ChatAgentToolNode  
from mlflow.pyfunc import ChatAgent  
from mlflow.types.agent import ChatAgentChunk, ChatAgentMessage, ChatAgentResponse, ChatContext  



# === Estado ===
class ConversationState(TypedDict):
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    metadata: Dict[str, Any]


# === Processadores ===
class MessageProcessor:
    """Responsável por processar e formatar mensagens"""
    
    @staticmethod
    def format_system_message(prompt: str) -> Dict[str, str]:
        return {"role": "system", "content": prompt}
    
    @staticmethod 
    def format_user_message(content: str) -> Dict[str, str]:
        return {"role": "user", "content": content}


class ToolOrchestrator:
    def __init__(self, selected_tools: List[str] = None):
        # Define todas as tools disponíveis
        selected_tools = selected_tools or ["system.ai.python_exec", "custom_sum", "custom_multiply"]
        
        # Separa Databricks das customizadas
        databricks_names = [name for name in selected_tools if name.startswith("system.")]
        custom_names = [name for name in selected_tools if not name.startswith("system.")]
        
        # Carrega tools do Databricks (se houver)
        db_tools = []
        if databricks_names:
            toolkit = UCFunctionToolkit(function_names=databricks_names)
            db_tools = toolkit.tools
        
        # Tools customizadas disponíveis
        @tool
        def custom_sum(a: int, b: int) -> int:
            """Soma dois números"""
            return a + b
        
        @tool
        def custom_multiply(a: int, b: int) -> int:
            """Multiplica dois números"""
            return a * b
        
        # Filtra apenas as customizadas selecionadas
        available_custom = {"custom_sum": custom_sum, "custom_multiply": custom_multiply}
        custom_tools = [available_custom[name] for name in custom_names if name in available_custom]
        
        # Mapeia por nome
        all_tools = db_tools + custom_tools
        self.tools_map = {tool.name: tool for tool in all_tools}

    def bind_to_llm(self, llm, tool_names: List[str] = None):
        """Bind ferramentas específicas no LLM"""
        if not tool_names:
            tools = list(self.tools_map.values())
            print(f"🔧 Usando TODAS as tools ({len(tools)})")
        else:
            tools = [self.tools_map[name] for name in tool_names if name in self.tools_map]
            print(f"🎯 Tools: {tool_names}")
        
        return llm.bind_tools(tools) if tools else llm

# === Agente Principal ===
class ConversationalAgent:
    """
    Agente conversacional genérico baseado em grafo.
    Funciona com qualquer LLM e conjunto de ferramentas.
    """
    
    def __init__(self, 
                 llm,
                 system_prompt: str = "Você é um assistente útil.",
                 config: Dict[str, Any] = None):
        
        self.llm = llm
        self.system_prompt = system_prompt
        self.message_processor = MessageProcessor()
        self.config = config or {}


        self.tool_orchestrator = ToolOrchestrator(tools or []) 
        # Constrói o grafo de processamento
        self.workflow = self._build_conversation_graph()
    
    # == Constrói o grafo de fluxo conversacional: estado do grafo, nó de entrada, outros nós ===
    def _build_conversation_graph(self) -> StateGraph:
        
        # == Passa a ConversationState (que define o estado do grafo) ===
        graph = StateGraph(ConversationState)        

        # Nós especializados
        # Lembrando que cada nó é uma função que:
        # Recebe o estado atual do grafo
        # Processa/transforma esse estado
        # Retorna o estado modificado
        # Entrada: state - o estado compartilhado do grafo (tipo ConversationState)

        graph.add_node("analyze_intent", self._analyze_intent)
        graph.add_node("math_processor", self._math_processor)
        graph.add_node("general_chat", self._general_chat)

        # Fluxo
        graph.set_entry_point("analyze_intent")
        graph.add_conditional_edges(
            "analyze_intent",
            self._route_to_processor,
            {
                "math": "math_processor",
                "general": "general_chat"
            }
        )

        graph.add_edge("math_processor", "general_chat")
        graph.add_edge("general_chat", END)
        
        return graph.compile()
    
    # == Nó para analisar intenção ==
    def _analyze_intent(self, state: ConversationState) -> ConversationState:
        """Analisa a intenção da mensagem do usuário"""
        # Pega última mensagem do usuário
        last_message = state["messages"][-1]["content"] if state["messages"] else ""
        
        # Análise simples - melhorar com LLM 
        # Por ora, detecta se tem operação matemática
        math_keywords = ["soma", "multiplica", "calcule", "+", "*", "vezes", "mais"]
        has_math = any(keyword in last_message.lower() for keyword in math_keywords)
        
        # Armazena a decisão no metadata
        return {
            **state,
            "metadata": {**state.get("metadata", {}), "intent": "math" if has_math else "general"}
        }

    # == Roteador baseado na análise ==
    def _route_to_processor(self, state: ConversationState) -> str:
        """Decide qual processador usar baseado na intenção"""
        intent = state.get("metadata", {}).get("intent", "general")
        return intent
    
    # == Processador de matemática ==
    def _math_processor(self, state: ConversationState) -> ConversationState:
        """Processa operações matemáticas com tools específicas"""
        # Prepara mensagens
        system_msg = {"role": "system", "content": "Você é um assistente matemático. Use as ferramentas disponíveis."}
        full_messages = [system_msg] + state["messages"]
        
        # Bind apenas tools matemáticas
        math_tools = ["custom_sum", "custom_multiply"]
        enhanced_llm = self.tool_orchestrator.bind_to_llm(self.llm, math_tools)
        
        # Invoca LLM
        response = enhanced_llm.invoke(full_messages)
        
        # Adiciona resposta ao estado
        assistant_msg = {"role": "assistant", "content": response.content}
        
        return {
            **state,
            "messages": state["messages"] + [assistant_msg],
            "metadata": {**state.get("metadata", {}), "processed_by": "math"}
        }

    def _general_chat(self, state: ConversationState) -> ConversationState:
        """Processa uma rodada de conversa com o LLM"""
        # Prepara mensagens com system prompt
        system_msg = self.message_processor.format_system_message(self.system_prompt)
        # 👇
        # Resultado: {"role": "system", "content": "Você é um assistente útil."} 
        
        full_messages = [system_msg] + state["messages"]
        # Exemplo do que pode estar em state["messages"]:
        # [
        #     {"role": "user", "content": "Olá, como você está?"},
        #     {"role": "assistant", "content": "Olá! Estou bem, obrigado!"},
        #     {"role": "user", "content": "Me explique Python"}
        # ]
        # 👇
        # O resultado final da concatenação [system_msg] + state["messages"] seria:
        # pythonfull_messages = [
        #     {"role": "system", "content": "Você é um assistente útil."},     # ← system_msg
        #     {"role": "user", "content": "Olá, como você está?"},             # ← do state
        #     {"role": "assistant", "content": "Olá! Estou bem, obrigado!"},   # ← do state (lembrando q assistant é o LLM) 
        #     {"role": "user", "content": "Me explique Python"}                # ← do state
        # ]



        # Verifica se já foi processado por math (evita reprocessar) --- EM DÚVIDA SE MANTENHO
        # if state.get("metadata", {}).get("processed_by") == "math":
        #     # Já tem resposta, apenas retorna
        #     return state

        node_tools = ["system.ai.python_exec"]  # ← NESTE EXEMPLO Só essa tool neste nó
        enhanced_llm = self.tool_orchestrator.bind_to_llm(self.llm, node_tools)

        response = enhanced_llm.invoke(full_messages)
        
        # Adiciona resposta ao estado
        assistant_msg = {"role": "assistant", "content": response.content}
        
        return {
            **state,
            "messages": state["messages"] + [assistant_msg]
        }
    
    def chat(self, message: str, 
             context: Dict[str, Any] = None
        """
        Interface principal para conversa com controle de ferramentas
        
        Args:
            message: Mensagem do usuário
            context: Contexto adicional
        """
        user_msg = self.message_processor.format_user_message(message)
        
        result = self.workflow.invoke({
            "messages": [user_msg],
            "context": context or {},
            "metadata": metadata
        })
        
        return result["messages"][-1]["content"]

# === Especialização ===
class DatabricksAgent(ConversationalAgent):
    """Especialização para ambiente Databricks"""
    
    def __init__(self, 
                 endpoint: str = "databricks-gpt-oss-20b",
                 system_prompt: str = "Você é um assistente especializado em Databricks.",
                 function_names: List[str] = None):
        
        llm = ChatDatabricks(endpoint=endpoint, temperature=0.2)

# Versão com MLflow (operacional)
# mlflow.langchain.autolog()
# agent = DatabricksAgent(system_prompt="Você é o Batman.")
# mlflow.models.set_model(agent)  # Pronto para deploy
