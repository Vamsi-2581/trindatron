import type { Copy } from './types'

/** Español — traducción comercial, no literal. Mismas reglas que en.ts. */
export const es: Copy = {
  meta: {
    title: 'TRINDATRON — No pidas solo una respuesta. Pon la IA a trabajar.',
    description:
      'La mayoría de las IA responden. TRINDATRON ejecuta: recibe el objetivo, coordina varias ' +
      'inteligencias, usa navegador, archivos y un ordenador propio, y devuelve el trabajo hecho.',
    ogLocale: 'es_ES',
    htmlLang: 'es',
  },

  nav: {
    product: 'Producto',
    work: 'Ejecución',
    agents: 'Agentes',
    reach: 'Computer use',
    intelligence: 'Inteligencias',
    pricing: 'Planes',
    signIn: 'Entrar',
    startFree: 'Empezar gratis',
    openMenu: 'Abrir menú',
    closeMenu: 'Cerrar menú',
    language: 'Idioma',
    languageHint: 'Elige tu idioma',
    backToApp: 'Abrir workspace',
  },

  hero: {
    kicker: 'Un solo lugar. Muchas inteligencias.',
    titleA: 'No pidas solo una respuesta.',
    titleB: 'Pon la IA a trabajar.',
    body:
      'Entrega el objetivo. TRINDATRON decide cómo llegar, pone la inteligencia adecuada en cada ' +
      'paso y usa herramientas reales — la web, archivos, un ordenador propio — hasta ' +
      'terminar el trabajo.',
    primary: 'Empezar gratis',
    secondary: 'Verlo trabajar',
    note: 'Plan gratuito para empezar. Sin tarjeta.',
  },

  inAction: {
    eyebrow: 'En acción',
    title: 'Pide un resultado. Míralo ocurrir.',
    body:
      'Una instrucción, y el trabajo avanza delante de ti — leído, planificado, ejecutado, ' +
      'comprobado. Ves cada paso según llega.',
    request: 'Compara cómo cobran sus planes los tres competidores más cercanos y ponlo en una tabla.',
    steps: [
      { label: 'Entiende la petición', detail: 'Identifica el objetivo y qué hace que una respuesta esté terminada.' },
      { label: 'Elige la inteligencia', detail: 'Lleva la tarea a la opción que mejor encaja con ella.' },
      { label: 'Reúne los datos', detail: 'Abre las páginas, las lee y recoge las cifras.' },
      { label: 'Construye la respuesta', detail: 'Escribe la comparación y comprueba que los números cuadran.' },
      { label: 'Entrega', detail: 'Devuelve la tabla con todas las fuentes que usó.' },
    ],
    resultLabel: 'Entregado',
    resultBody: 'Comparación de precios — tres competidores, plan a plan.',
    resultMeta: 'Tabla · 5 fuentes · lista para usar',
  },

  newWay: {
    eyebrow: 'Otro tipo de IA',
    title: 'Hay más en la IA que una caja de chat.',
    body:
      'Los asistentes que todos conocen están hechos para responder. TRINDATRON está hecho para ' +
      'terminar — un lugar donde entra el objetivo y sale el trabajo hecho.',
    fromLabel: 'A lo que estás acostumbrado',
    toLabel: 'Lo que hace TRINDATRON',
    rows: [
      { from: 'Recibes una respuesta', to: 'Recibes el trabajo terminado' },
      { from: 'Eliges el modelo', to: 'La inteligencia adecuada se elige en cada paso' },
      { from: 'Copias, pegas y lo rehaces tú', to: 'Usa el navegador, los archivos y un ordenador por ti' },
      { from: 'Cada conversación empieza de cero', to: 'Proyectos y memoria llevan tu contexto adelante' },
      { from: 'Un modelo, una opinión', to: 'Muchas inteligencias, un resultado' },
    ],
  },

  intelligences: {
    eyebrow: 'Inteligencias',
    title: 'Muchas inteligencias. Un resultado.',
    body:
      'Hablas con un solo lugar. Detrás, cada paso va a la inteligencia que mejor lo resuelve — y ' +
      'cuando aparecen opciones mejores, entran al trabajo sin cambiar nada de tu lado.',
    points: [
      {
        title: 'La adecuada para cada tarea',
        body: 'Una respuesta rápida y un análisis largo son trabajos distintos. Cada uno va donde rinde más.',
      },
      {
        title: 'Siempre al día',
        body: 'Las nuevas inteligencias entran al workspace según llegan. Tu experiencia sigue igual.',
      },
      {
        title: 'Nada que elegir',
        body: 'Sin selector de modelo ni ajustes que mantener. Describes el resultado; la elección está resuelta.',
      },
    ],
    caption: 'Cada petición va a la inteligencia que encaja con ella.',
    youLabel: 'Tú',
    coreLabel: 'TRINDATRON',
    fieldLabel: 'Inteligencias disponibles',
  },

  work: {
    eyebrow: 'Ejecución',
    title: 'Delega la tarea. Quédate con la última palabra.',
    body:
      'El trabajo real es más que una respuesta. TRINDATRON lleva un objetivo hasta el resultado, ' +
      'mostrando cada paso — y parando contigo en los momentos que importan.',
    steps: [
      { label: 'Entender', detail: 'Convierte tu objetivo en lo que tiene que ser cierto al final.' },
      { label: 'Planificar', detail: 'Lo divide en pasos y decide en qué orden darlos.' },
      { label: 'Ejecutar', detail: 'Hace el trabajo con herramientas reales — web, archivos, terminal.' },
      { label: 'Comprobar', detail: 'Revisa el resultado contra el objetivo antes de darlo por hecho.' },
      { label: 'Entregar', detail: 'Devuelve el trabajo terminado y el camino que siguió.' },
    ],
    controlTitle: 'Tú mantienes el control',
    controlBody:
      'Leer es automático. Actuar pregunta antes: escribir un archivo, ejecutar un comando o enviar ' +
      'algo en una página detiene la ejecución y espera tu aprobación.',
    approve: 'Aprobar',
    review: 'Revisar',
  },

  agents: {
    eyebrow: 'Agentes',
    title: 'Da la dirección. Deja que él ejecute.',
    body:
      'Crea un agente para el trabajo que vuelve cada semana y dale el objetivo en lugar de las ' +
      'instrucciones. Él planifica, ejecuta e informa.',
    points: [
      {
        title: 'Delega un resultado',
        body: 'Describe cómo es un buen resultado. Averiguar los pasos es tarea del agente.',
      },
      {
        title: 'Trabaja sin ti',
        body: 'Los agentes programados se activan solos, revisan la situación y actúan solo si hay algo que hacer.',
      },
      {
        title: 'Rinde cuentas',
        body: 'Cada ejecución deja un rastro legible: qué hizo, qué herramientas usó, qué salió.',
      },
    ],
  },

  reach: {
    eyebrow: 'Computer use',
    title: 'IA que trabaja más allá del chat.',
    body:
      'TRINDATRON tiene un ordenador propio — un navegador que navega de verdad, archivos que ' +
      'escribe, un terminal que ejecuta y una pantalla que ve y opera. Toda acción que cambia algo ' +
      'espera tu aprobación.',
    liveLabel: 'Funcionando hoy',
    nextLabel: 'En despliegue',
    live: [
      {
        title: 'La web, de primera mano',
        body: 'Abre páginas reales, las lee, hace clic y rellena formularios para llegar a lo que necesita.',
      },
      {
        title: 'Archivos y terminal',
        body: 'Lee y escribe archivos y ejecuta comandos dentro de un workspace aislado propio.',
      },
      {
        title: 'Pantallas que ve',
        body: 'Captura cómo está realmente la página y comprueba el resultado contra lo que hay de verdad.',
      },
    ],
    next: [
      {
        title: 'Una pantalla que opera',
        body:
          'Pantalla, ratón y teclado propios, dentro de un entorno sellado — construido, con ' +
          'aprobación obligatoria y en pruebas, llegando a las cuentas según se libera capacidad.',
      },
      {
        title: 'Las herramientas que ya usas',
        body: 'Llevando ese mismo alcance a las aplicaciones donde tu trabajo ya ocurre.',
      },
    ],
  },

  memory: {
    eyebrow: 'Continuidad',
    title: 'Recuerda el trabajo.',
    body:
      'Cada conversación aprovecha la anterior. Los proyectos guardan su propio contexto, y lo que ' +
      'estableciste hace semanas sigue ahí cuando vuelves.',
    points: [
      { title: 'Proyectos', body: 'Trabajo agrupado como tú lo piensas, cada uno con su contexto e historial.' },
      { title: 'Memoria', body: 'Lo que contaste una vez se mantiene sabido — preferencias, decisiones, la forma de tu trabajo.' },
      { title: 'Continuidad', body: 'Retoma días después, donde lo dejaste, sin volver a explicar el contexto.' },
    ],
  },

  why: {
    eyebrow: 'Por qué TRINDATRON',
    title: 'Hecho para trabajo que tiene que llegar.',
    body: 'Lo que hace fiable un resultado, integrado en la forma en que ocurre el trabajo.',
    points: [
      { title: 'La inteligencia adecuada para la tarea', body: 'Cada paso va a quien mejor lo resuelve.' },
      { title: 'El trabajo sigue avanzando', body: 'Cuando un camino está ocupado, la ejecución continúa por otro.' },
      { title: 'Tu workspace es tuyo', body: 'Sesiones, proyectos y archivos están aislados por cuenta y se verifican en cada petición.' },
      { title: 'Tú apruebas lo que importa', body: 'Escribir, ejecutar y enviar siempre te esperan.' },
      { title: 'Entorno controlado', body: 'La ejecución ocurre en un workspace aislado con acceso de red restringido.' },
      { title: 'Mira cómo se hizo', body: 'Cada paso indica la herramienta que usó, así el resultado se puede rastrear.' },
    ],
  },

  pricing: {
    eyebrow: 'Planes',
    title: 'Empieza gratis. Sube cuando se pague solo.',
    body:
      'Pagas por el trabajo que sacas de la plataforma, no por modelo. Cambia de plan o cancela ' +
      'cuando quieras.',
    perMonth: '/mes',
    free: 'Gratis',
    recommended: 'El más elegido',
    plans: {
      free: {
        tagline: 'Explorar',
        volume: 'Una muestra del workspace',
        features: [
          { label: 'Conversación, búsqueda y memoria', included: true },
          { label: 'Hasta 3 proyectos', included: true },
          { label: 'Nivel esencial de inteligencia', included: true },
          { label: 'Agentes que ejecutan trabajo', included: false },
          { label: 'Navegador, archivos y terminal', included: false },
        ],
        cta: 'Empezar gratis',
      },
      start: {
        tagline: 'Coger ritmo',
        volume: 'Trabajo del día a día',
        features: [
          { label: 'Agentes que ejecutan trabajo', included: true },
          { label: 'Navegador, archivos y terminal', included: true },
          { label: 'Nivel avanzado de inteligencia', included: true },
          { label: 'Hasta 10 proyectos y 5 agentes', included: true },
          { label: 'Sesiones que se renuevan cada 5 horas', included: true },
        ],
        cta: 'Elegir START',
      },
      pro: {
        tagline: 'Trabajar profesionalmente',
        volume: 'Profesional, todo el día',
        features: [
          { label: 'Todo lo de START', included: true },
          { label: 'Ejecución con aprobación (WORK)', included: true },
          { label: 'Hasta 50 proyectos y 20 agentes', included: true },
          { label: 'Mucho más volumen de ejecución', included: true },
          { label: 'Sesiones que se renuevan cada 5 horas', included: true },
        ],
        cta: 'Elegir PRO',
      },
      max: {
        tagline: 'Ir más lejos',
        volume: 'Intensivo, sin mirar el reloj',
        features: [
          { label: 'Todo lo de PRO', included: true },
          { label: 'Inteligencias premium', included: true },
          { label: 'Tareas más largas por petición', included: true },
          { label: 'Proyectos y agentes sin tope fijo', included: true },
          { label: 'Sesiones que se renuevan cada 5 horas', included: true },
        ],
        cta: 'Elegir MAX',
      },
      team: {
        tagline: 'Escalar en equipo',
        volume: 'Intensivo, compartido por el equipo',
        features: [
          { label: 'Todo lo de MAX, para el equipo', included: true },
          { label: 'Un volumen común compartido', included: true },
          { label: 'Workspace centralizado', included: true },
          { label: 'Onboarding dedicado', included: true },
          { label: 'Sesiones que se renuevan cada 5 horas', included: true },
        ],
        cta: 'Elegir TEAM',
      },
    },
    windowNote:
      'Los planes de pago funcionan en sesiones de cinco horas que se renuevan solas, con límites ' +
      'diarios y semanales detrás. Se te avisa cuando una ventana está cerca y cuándo vuelve.',
    currencyNote: 'Precios en dólares estadounidenses.',
  },

  closing: {
    title: 'Deja de preguntar. Empieza a terminar.',
    body:
      'Trae una tarea real — la que ibas a hacer tú mismo — y mira lo que vuelve. ' +
      'Lleva menos tiempo que elegir un modelo.',
    primary: 'Empezar gratis',
    secondary: 'Entrar',
  },

  footer: {
    tagline: 'El workspace de IA que hace el trabajo, no solo la conversación.',
    product: 'Producto',
    company: 'Empresa',
    signIn: 'Entrar',
    rights: 'Todos los derechos reservados.',
  },
}
