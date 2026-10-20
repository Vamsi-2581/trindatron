import type { Copy } from './types'

/** Português — tradução comercial, não literal. Mesmas regras do dicionário en.ts. */
export const pt: Copy = {
  meta: {
    title: 'TRINDATRON — Não peça só uma resposta. Coloque a IA para trabalhar.',
    description:
      'A maioria das IAs responde. O TRINDATRON executa: recebe o objetivo, coordena várias ' +
      'inteligências, usa navegador, arquivos e um computador próprio, e devolve o trabalho pronto.',
    ogLocale: 'pt_BR',
    htmlLang: 'pt-BR',
  },

  nav: {
    product: 'Produto',
    work: 'Execução',
    agents: 'Agentes',
    reach: 'Computer use',
    intelligence: 'Inteligências',
    pricing: 'Planos',
    signIn: 'Entrar',
    startFree: 'Começar grátis',
    openMenu: 'Abrir menu',
    closeMenu: 'Fechar menu',
    language: 'Idioma',
    languageHint: 'Escolha seu idioma',
    backToApp: 'Abrir workspace',
  },

  hero: {
    kicker: 'Um só lugar. Muitas inteligências.',
    titleA: 'Não peça só uma resposta.',
    titleB: 'Coloque a IA para trabalhar.',
    body:
      'Entregue o objetivo. O TRINDATRON decide como chegar lá, coloca a inteligência certa em ' +
      'cada etapa e usa ferramentas reais — web, arquivos, um computador próprio — até o ' +
      'trabalho ficar pronto.',
    primary: 'Começar grátis',
    secondary: 'Ver trabalhando',
    note: 'Plano gratuito para começar. Sem cartão de crédito.',
  },

  inAction: {
    eyebrow: 'Em ação',
    title: 'Peça um resultado. Veja acontecer.',
    body:
      'Uma instrução, e o trabalho corre na sua frente — lido, planejado, executado, conferido. ' +
      'Você acompanha cada etapa.',
    request: 'Compare como os três concorrentes mais próximos cobram pelos planos e monte uma tabela.',
    steps: [
      { label: 'Entende o pedido', detail: 'Identifica o objetivo e o que caracteriza uma resposta pronta.' },
      { label: 'Escolhe a inteligência', detail: 'Leva a tarefa para a opção mais adequada a ela.' },
      { label: 'Reúne os fatos', detail: 'Abre as páginas, lê o conteúdo e coleta os números.' },
      { label: 'Monta a resposta', detail: 'Escreve a comparação e confere se os valores fecham.' },
      { label: 'Entrega', detail: 'Devolve a tabela com todas as fontes utilizadas.' },
    ],
    resultLabel: 'Entregue',
    resultBody: 'Comparação de preços — três concorrentes, plano a plano.',
    resultMeta: 'Tabela · 5 fontes · pronta para usar',
  },

  newWay: {
    eyebrow: 'Um outro tipo de IA',
    title: 'Existe mais em IA do que uma caixa de conversa.',
    body:
      'Os assistentes que todo mundo conhece foram feitos para responder. O TRINDATRON foi feito ' +
      'para concluir — um lugar onde entra o objetivo e sai o trabalho pronto.',
    fromLabel: 'O que você está acostumado',
    toLabel: 'O que o TRINDATRON faz',
    rows: [
      { from: 'Você recebe uma resposta', to: 'Você recebe o trabalho pronto' },
      { from: 'Você escolhe o modelo', to: 'A inteligência certa é escolhida em cada etapa' },
      { from: 'Você copia, cola e refaz na mão', to: 'Ele usa navegador, arquivos e um computador por você' },
      { from: 'Cada conversa começa do zero', to: 'Projetos e memória levam o seu contexto adiante' },
      { from: 'Um modelo, uma opinião', to: 'Muitas inteligências, um resultado' },
    ],
  },

  intelligences: {
    eyebrow: 'Inteligências',
    title: 'Muitas inteligências. Um resultado.',
    body:
      'Você fala com um só lugar. Por trás, cada etapa vai para a inteligência mais adequada a ela — ' +
      'e quando surgem opções melhores, elas entram no trabalho sem mudar nada do seu lado.',
    points: [
      {
        title: 'A adequada para cada tarefa',
        body: 'Uma resposta rápida e uma análise longa são trabalhos diferentes. Cada uma vai onde rende mais.',
      },
      {
        title: 'Sempre atualizado',
        body: 'Novas inteligências entram no workspace conforme surgem. A sua experiência continua igual.',
      },
      {
        title: 'Nada para escolher',
        body: 'Sem seletor de modelo, sem configuração para manter. Você descreve o resultado; a escolha é feita.',
      },
    ],
    caption: 'Cada pedido vai para a inteligência que combina com ele.',
    youLabel: 'Você',
    coreLabel: 'TRINDATRON',
    fieldLabel: 'Inteligências disponíveis',
  },

  work: {
    eyebrow: 'Execução',
    title: 'Delegue a tarefa. Fique com a última palavra.',
    body:
      'Trabalho de verdade é mais do que uma resposta. O TRINDATRON leva um objetivo até o resultado, ' +
      'mostrando cada etapa — e parando com você nos momentos que importam.',
    steps: [
      { label: 'Entender', detail: 'Transforma o seu objetivo no que precisa ser verdade no final.' },
      { label: 'Planejar', detail: 'Divide em etapas e decide a ordem de execução.' },
      { label: 'Executar', detail: 'Faz o trabalho com ferramentas reais — web, arquivos, terminal.' },
      { label: 'Conferir', detail: 'Revisa o resultado contra o objetivo antes de considerar pronto.' },
      { label: 'Entregar', detail: 'Devolve o trabalho pronto e o caminho que percorreu.' },
    ],
    controlTitle: 'Você no controle',
    controlBody:
      'Ler é automático. Agir pergunta antes: escrever arquivo, rodar comando ou enviar algo em uma ' +
      'página faz a execução parar e esperar a sua aprovação.',
    approve: 'Aprovar',
    review: 'Revisar',
  },

  agents: {
    eyebrow: 'Agentes',
    title: 'Dê a direção. Deixe que ele executa.',
    body:
      'Crie um agente para o trabalho que volta toda semana e entregue a ele o objetivo em vez das ' +
      'instruções. Ele planeja, executa e reporta.',
    points: [
      {
        title: 'Delegue um resultado',
        body: 'Descreva como é um bom resultado. Descobrir as etapas é trabalho do agente.',
      },
      {
        title: 'Trabalha sem você',
        body: 'Agentes agendados acordam sozinhos, verificam a situação e agem só quando há o que fazer.',
      },
      {
        title: 'Presta contas',
        body: 'Cada execução deixa um rastro legível: o que fez, quais ferramentas usou, o que saiu.',
      },
    ],
  },

  reach: {
    eyebrow: 'Computer use',
    title: 'IA que trabalha além da conversa.',
    body:
      'O TRINDATRON tem um computador próprio — um navegador que ele navega de verdade, arquivos ' +
      'que ele escreve, um terminal que ele roda e uma tela que ele enxerga e opera. Toda ação que ' +
      'muda alguma coisa espera a sua aprovação.',
    liveLabel: 'Funcionando hoje',
    nextLabel: 'Em liberação',
    live: [
      {
        title: 'A web, em primeira mão',
        body: 'Abre páginas reais, lê o conteúdo, clica e preenche formulários para chegar ao que precisa.',
      },
      {
        title: 'Arquivos e terminal',
        body: 'Lê e escreve arquivos e roda comandos dentro de um workspace isolado só dele.',
      },
      {
        title: 'Telas que ele enxerga',
        body: 'Captura como a página realmente está, e confere o resultado contra o que existe de fato.',
      },
    ],
    next: [
      {
        title: 'Uma tela que ele opera',
        body:
          'Tela, mouse e teclado próprios, dentro de um ambiente lacrado — construído, com aprovação ' +
          'obrigatória e em teste, chegando às contas conforme a capacidade é liberada.',
      },
      {
        title: 'As ferramentas que você já usa',
        body: 'Levando esse mesmo alcance para os aplicativos onde o seu trabalho já acontece.',
      },
    ],
  },

  memory: {
    eyebrow: 'Continuidade',
    title: 'Ele lembra do trabalho.',
    body:
      'Cada conversa aproveita a anterior. Projetos guardam o próprio contexto, e o que você ' +
      'estabeleceu semanas atrás continua lá quando você volta.',
    points: [
      { title: 'Projetos', body: 'Trabalho organizado do seu jeito, cada um com contexto e histórico próprios.' },
      { title: 'Memória', body: 'O que você contou uma vez continua sabido — preferências, decisões, o formato do seu trabalho.' },
      { title: 'Continuidade', body: 'Retome dias depois, do ponto onde parou, sem repetir o contexto.' },
    ],
  },

  why: {
    eyebrow: 'Por que TRINDATRON',
    title: 'Feito para trabalho que precisa chegar.',
    body: 'O que torna um resultado confiável, embutido na forma como o trabalho acontece.',
    points: [
      { title: 'A inteligência certa para a tarefa', body: 'Cada etapa vai para quem resolve melhor.' },
      { title: 'O trabalho continua andando', body: 'Quando um caminho está ocupado, a execução segue por outro.' },
      { title: 'Seu workspace é seu', body: 'Sessões, projetos e arquivos são isolados por conta e verificados a cada requisição.' },
      { title: 'Você aprova o que importa', body: 'Escrever, executar e enviar sempre esperam por você.' },
      { title: 'Ambiente controlado', body: 'A execução acontece em workspace isolado, com acesso de rede restrito.' },
      { title: 'Veja como foi feito', body: 'Cada etapa mostra a ferramenta usada, então dá para refazer o caminho.' },
    ],
  },

  pricing: {
    eyebrow: 'Planos',
    title: 'Comece grátis. Suba quando se pagar.',
    body:
      'Você paga pelo trabalho que tira da plataforma, não por modelo. Troque de plano ou cancele ' +
      'quando quiser.',
    perMonth: '/mês',
    free: 'Grátis',
    recommended: 'Mais escolhido',
    plans: {
      free: {
        tagline: 'Conhecer',
        volume: 'Uma amostra do workspace',
        features: [
          { label: 'Conversa, pesquisa e memória', included: true },
          { label: 'Até 3 projetos', included: true },
          { label: 'Faixa essencial de inteligência', included: true },
          { label: 'Agentes que executam trabalho', included: false },
          { label: 'Navegador, arquivos e terminal', included: false },
        ],
        cta: 'Começar grátis',
      },
      start: {
        tagline: 'Ganhar ritmo',
        volume: 'Trabalho do dia a dia',
        features: [
          { label: 'Agentes que executam trabalho', included: true },
          { label: 'Navegador, arquivos e terminal', included: true },
          { label: 'Faixa avançada de inteligência', included: true },
          { label: 'Até 10 projetos e 5 agentes', included: true },
          { label: 'Sessões que renovam a cada 5 horas', included: true },
        ],
        cta: 'Escolher START',
      },
      pro: {
        tagline: 'Trabalhar profissionalmente',
        volume: 'Profissional, o dia inteiro',
        features: [
          { label: 'Tudo do START', included: true },
          { label: 'Execução com aprovação (WORK)', included: true },
          { label: 'Até 50 projetos e 20 agentes', included: true },
          { label: 'Muito mais volume de execução', included: true },
          { label: 'Sessões que renovam a cada 5 horas', included: true },
        ],
        cta: 'Escolher PRO',
      },
      max: {
        tagline: 'Ir além',
        volume: 'Intenso, sem olhar o relógio',
        features: [
          { label: 'Tudo do PRO', included: true },
          { label: 'Inteligências premium', included: true },
          { label: 'Tarefas mais longas por pedido', included: true },
          { label: 'Projetos e agentes sem limite fixo', included: true },
          { label: 'Sessões que renovam a cada 5 horas', included: true },
        ],
        cta: 'Escolher MAX',
      },
      team: {
        tagline: 'Escalar junto',
        volume: 'Intenso, compartilhado pela equipe',
        features: [
          { label: 'Tudo do MAX, para a equipe', included: true },
          { label: 'Um volume comum compartilhado', included: true },
          { label: 'Workspace centralizado', included: true },
          { label: 'Onboarding dedicado', included: true },
          { label: 'Sessões que renovam a cada 5 horas', included: true },
        ],
        cta: 'Escolher TEAM',
      },
    },
    windowNote:
      'Os planos pagos trabalham em sessões de cinco horas que se renovam sozinhas, com limites ' +
      'diários e semanais por trás. Você é avisado quando a janela está perto e quando ela volta.',
    currencyNote: 'Preços em dólar americano.',
  },

  closing: {
    title: 'Pare de perguntar. Comece a concluir.',
    body:
      'Traga uma tarefa real — aquela que você ia fazer na mão — e veja o que volta. ' +
      'Leva menos tempo do que escolher um modelo.',
    primary: 'Começar grátis',
    secondary: 'Entrar',
  },

  footer: {
    tagline: 'O workspace de IA que faz o trabalho, não só a conversa.',
    product: 'Produto',
    company: 'Empresa',
    signIn: 'Entrar',
    rights: 'Todos os direitos reservados.',
  },
}
