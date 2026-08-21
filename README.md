# Backend de gestión de gastos

El proyecto contiene el backend de una aplicación para la gestión de gastos corporativos. Está desarrollado con **Django** y **Django REST Framework**, utiliza **PostgreSQL** como sistema gestor de base de datos y ofrece una **API REST**.

La aplicación permite gestionar usuarios, categorías, atributos organizativos, informes y gastos. También incorpora un sistema de reglas y pasos de aprobación que permite definir el flujo que debe seguir cada informe antes de ser aprobado.

Los usuarios pueden crear informes, añadir gastos, enviarlos para su aprobación y, dependiendo de su rol, aprobarlos, rechazarlos o delegar su revisión. Los administradores disponen además del panel de administración de Django para gestionar los principales datos de la aplicación.

La documentación de los endpoints se encuentra disponible mediante **Swagger**.

## Estructura general

El backend se encuentra dividido en distintas aplicaciones y módulos según su responsabilidad principal:

* `config/`: configuración general de Django, rutas principales, ajustes del proyecto y endpoints de documentación y comprobación del servicio.
* `users/`: gestión de usuarios, autenticación y roles.
* `expenses/`: gestión de informes, gastos, categorías, atributos personalizados y reglas relacionadas con los gastos.
* `approvals/`: gestión de reglas, pasos y acciones relacionadas con el proceso de aprobación.
* `common/`: funcionalidades compartidas entre las distintas aplicaciones.

Esta separación permite mantener organizada la lógica de negocio y diferenciar las responsabilidades de cada parte de la aplicación.

## Requisitos

Para ejecutar el proyecto mediante contenedores es necesario disponer de:

* Docker
* Docker Compose

No es necesario instalar Python ni PostgreSQL directamente en el equipo, ya que ambos servicios se ejecutan dentro de los contenedores.

El proyecto utiliza **Python 3.12** y **PostgreSQL 16** dentro de su entorno de ejecución.

## Instalación y ejecución

En primer lugar, se debe descomprimir el proyecto y abrir una terminal dentro de la carpeta raíz, donde se encuentra el archivo `docker-compose.yml`.

Para construir las imágenes e iniciar la aplicación se utiliza el siguiente comando:

```bash
docker compose up --build
```

Este comando inicia los servicios necesarios para el funcionamiento de la aplicación:

* La base de datos PostgreSQL.
* El backend desarrollado con Django.
* La aplicación de las migraciones existentes.

El backend queda disponible en el puerto `8000`.

La base de datos dispone de un volumen persistente, por lo que los datos almacenados no se eliminan cuando se detienen los contenedores.

## Creación del superusuario

Una vez que los contenedores estén en funcionamiento, debe crearse un superusuario para acceder al panel de administración de Django:

```bash
docker compose exec backend python manage.py createsuperuser
```

Durante este proceso se solicitarán los datos necesarios para crear la cuenta administrativa.

El superusuario permite acceder al panel de administración y gestionar usuarios, roles, categorías, atributos organizativos, reglas de aprobación y otros datos necesarios para utilizar la aplicación.

## Acceso a la aplicación

Con el proyecto en ejecución, se puede acceder a los siguientes recursos:

* Panel de administración de Django: `http://localhost:8000/admin/`
* Documentación Swagger de la API: `http://localhost:8000/api/docs/`
* Esquema OpenAPI: `http://localhost:8000/api/schema/`
* Comprobación del estado del servicio: `http://localhost:8000/api/health/`

Desde el panel de administración es posible crear y modificar los principales datos de la aplicación.

Swagger puede utilizarse para consultar y probar los endpoints disponibles directamente desde el navegador. También es posible utilizar herramientas externas como Postman.

## Autenticación

La API utiliza autenticación mediante **JSON Web Tokens (JWT)**.

El inicio de sesión permite obtener un token de acceso que debe enviarse en las peticiones realizadas a los endpoints protegidos.

Las principales rutas relacionadas con la autenticación son:

* `/api/auth/login/`: inicio de sesión.
* `/api/auth/refresh/`: renovación del token de acceso.
* `/api/auth/logout/`: cierre de sesión e invalidación del token correspondiente.
* `/api/me/`: consulta de la información del usuario autenticado.

En Swagger, una vez obtenido el token de acceso, puede utilizarse la opción **Authorize** para autenticarse y probar los endpoints protegidos.

## Principales endpoints

La API se encuentra organizada en distintos grupos de recursos. Entre los principales endpoints se encuentran:

* `/api/users/`: gestión de usuarios.
* `/api/org-attributes/`: gestión de atributos organizativos.
* `/api/categories/`: gestión de categorías de gasto.
* `/api/warning-rules/`: gestión de reglas de aviso.
* `/api/approval-rules/`: gestión de reglas de aprobación.
* `/api/reports/`: gestión de informes de gastos.
* `/api/expenses/`: gestión de gastos.
* `/api/reports/{reportId}/approval-steps/`: consulta y gestión de los pasos de aprobación.
* `/api/reports/{reportId}/approval-preview/`: previsualización del flujo de aprobación.
* `/api/reports/{reportId}/submit/`: envío de un informe.
* `/api/reports/{reportId}/approve/`: aprobación de un informe.
* `/api/reports/{reportId}/reject/`: rechazo de un informe.
* `/api/reports/{reportId}/delegate/`: delegación de una aprobación.
* `/api/reports/{reportId}/return-to-draft/`: devolución de un informe al estado borrador.
* `/api/reports/{reportId}/mark-paid/`: marcado de un informe aprobado como pagado.

La documentación completa de las rutas, parámetros y cuerpos de las peticiones puede consultarse desde Swagger.

## Flujo general de un informe

El funcionamiento principal de la aplicación se basa en informes de gastos.

De forma resumida, el flujo es el siguiente:

1. Un usuario crea un informe en estado borrador.
2. Se añaden uno o varios gastos al informe.
3. Mientras el informe permanezca en estado borrador, sus gastos pueden modificarse.
4. El usuario envía el informe para su aprobación.
5. Al enviarlo, el sistema evalúa las reglas de aprobación configuradas y determina los pasos de aprobación correspondientes.
6. Los usuarios autorizados pueden aprobar, rechazar o delegar la revisión del informe.
7. Una vez completados los pasos necesarios, el informe pasa a estar aprobado.
8. Finalmente, un administrador puede marcar el informe aprobado como pagado.

El sistema mantiene además un registro de los eventos producidos durante el proceso, permitiendo conservar un historial de las principales acciones realizadas sobre cada informe.

## Reglas de aprobación

Los administradores pueden configurar reglas que determinan qué usuarios deben intervenir en la aprobación de un informe.

Estas reglas pueden tener en cuenta diferentes características, como la categoría o el importe de un gasto, así como determinados atributos organizativos del usuario.

Cuando se envía un informe, el backend evalúa las reglas activas y genera los pasos de aprobación correspondientes en el orden establecido.

También es posible consultar previamente el flujo que se generaría para un informe mediante el endpoint:

```text
/api/reports/{reportId}/approval-preview/
```

## Variables de entorno

La configuración de la aplicación se realiza mediante variables de entorno.

El proyecto incluye un archivo `.env.example` que sirve como referencia de las variables disponibles. Entre ellas se encuentran la configuración de Django, la conexión con PostgreSQL, los orígenes permitidos mediante CORS y la duración de los tokens JWT.

Las credenciales y secretos utilizados en entornos reales no deben almacenarse directamente en el repositorio.

Al ejecutar el proyecto mediante Docker Compose, las variables necesarias para el entorno de desarrollo son proporcionadas por la configuración del proyecto.

## Detener la aplicación

Para detener los contenedores se utiliza el siguiente comando:

```bash
docker compose down
```

Este comando detiene y elimina los contenedores utilizados por la aplicación.

Los datos almacenados en PostgreSQL se mantienen en un volumen de Docker, por lo que no se eliminan al detener la aplicación.

Si posteriormente se vuelve a ejecutar:

```bash
docker compose up
```

la aplicación utilizará nuevamente los datos almacenados anteriormente.
