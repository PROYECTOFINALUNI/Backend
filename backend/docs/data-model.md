# Data model

UML class diagram of the persistence layer: entities, `TextChoices` enums, inheritance, and
foreign-key relationships. Each association is annotated with its cardinality and the `on_delete`
behavior configured on the foreign key.

- `CASCADE` — children are removed with their parent (report → expenses, report → approval rows,
  expense → warnings).
- `PROTECT` — the referenced row cannot be deleted while it is still referenced (users, categories).
- `SET_NULL` — deleting a `WarningRule` keeps the historical `ExpenseWarning` snapshot, nulling the
  link.

All core entities extend the abstract `TimeStampedModel` (`created_at` / `updated_at`). `User` also
extends Django's `AbstractBaseUser` + `PermissionsMixin`. `ExpenseWarning` and `ApprovalEvent` are
immutable audit rows and only carry `created_at`, so they do not inherit `TimeStampedModel`.

![Data model](data-model.png)

## Mermaid

```mermaid
classDiagram
    direction LR

    class TimeStampedModel {
        <<abstract>>
        +datetime created_at
        +datetime updated_at
    }

    class User {
        +UUID id
        +str email  «unique, ci»
        +str full_name
        +Role role
        +bool is_active
        +bool is_staff
        +bool is_superuser
        +datetime created_at
        +datetime updated_at
    }
    class Role {
        <<enumeration>>
        EMPLOYEE
        APPROVER
        ADMIN
    }

    class ExpenseCategory {
        +UUID id
        +str code  «unique, upper»
        +str name
        +bool active
    }

    class ExpenseReport {
        +UUID id
        +str title
        +Status status
        +datetime submitted_at
    }
    class ReportStatus {
        <<enumeration>>
        DRAFT
        SUBMITTED
        APPROVED
        REJECTED
        PAID
    }

    class Expense {
        +UUID id
        +str merchant
        +date expense_date
        +str currency
        +bigint amount_minor
        +int tax_rate_bps
        +bigint tax_amount_minor
        +bigint total_amount_minor
        +bool tax_included
        +Status status
    }

    class WarningRule {
        +UUID id
        +str name
        +str currency
        +bigint threshold_minor
        +Severity severity
        +str message
        +bool active
    }
    class Severity {
        <<enumeration>>
        INFO
        WARNING
        BLOCKING
    }

    class ExpenseWarning {
        +UUID id
        +Severity severity
        +str message
        +datetime created_at
    }

    class ApprovalStep {
        +UUID id
        +int step_order
        +Status status
        +datetime decided_at
        +str comment
        +datetime created_at
        +datetime updated_at
    }
    class StepStatus {
        <<enumeration>>
        PENDING
        APPROVED
        REJECTED
        SKIPPED
    }

    class ApprovalEvent {
        +UUID id
        +Action action
        +str comment
        +datetime created_at
    }
    class Action {
        <<enumeration>>
        SUBMITTED
        APPROVED
        REJECTED
        COMMENTED
        RETURNED_TO_DRAFT
        PAID
    }

    TimeStampedModel <|-- User
    TimeStampedModel <|-- ExpenseCategory
    TimeStampedModel <|-- ExpenseReport
    TimeStampedModel <|-- Expense
    TimeStampedModel <|-- WarningRule
    TimeStampedModel <|-- ApprovalStep

    User --> Role
    ExpenseReport --> ReportStatus
    Expense --> ReportStatus
    WarningRule --> Severity
    ExpenseWarning --> Severity
    ApprovalStep --> StepStatus
    ApprovalEvent --> Action

    User "1" --> "0..*" ExpenseReport : owns (PROTECT)
    User "1" --> "0..*" Expense : incurs (PROTECT)
    ExpenseReport "1" --> "1..*" Expense : contains (CASCADE)
    ExpenseCategory "0..1" --> "0..*" Expense : categorizes (PROTECT)
    ExpenseCategory "0..1" --> "0..*" WarningRule : scopes (PROTECT)
    Expense "1" --> "0..*" ExpenseWarning : raises (CASCADE)
    WarningRule "0..1" --> "0..*" ExpenseWarning : snapshot of (SET_NULL)
    ExpenseReport "1" --> "0..*" ApprovalStep : has (CASCADE)
    User "1" --> "0..*" ApprovalStep : approves (PROTECT)
    ExpenseReport "1" --> "0..*" ApprovalEvent : audited by (CASCADE)
    User "1" --> "0..*" ApprovalEvent : acts on (PROTECT)
```

## PlantUML

The source also lives in [`data-model.puml`](data-model.puml). Render it with:

```bash
java -jar plantuml.jar -tpng backend/docs/data-model.puml
```

```plantuml
@startuml data-model
skinparam classAttributeIconSize 0
skinparam shadowing false
hide empty members

abstract class TimeStampedModel {
  +created_at : datetime
  +updated_at : datetime
}

class User {
  +id : UUID
  +email : str «unique, ci»
  +full_name : str
  +role : Role
  +is_active : bool
  +is_staff : bool
  +is_superuser : bool
  +created_at : datetime
  +updated_at : datetime
}
enum Role {
  EMPLOYEE
  APPROVER
  ADMIN
}

class ExpenseCategory {
  +id : UUID
  +code : str «unique, upper»
  +name : str
  +active : bool
}

class ExpenseReport {
  +id : UUID
  +title : str
  +status : ReportStatus
  +submitted_at : datetime
}
enum ReportStatus {
  DRAFT
  SUBMITTED
  APPROVED
  REJECTED
  PAID
}

class Expense {
  +id : UUID
  +merchant : str
  +expense_date : date
  +currency : str
  +amount_minor : bigint
  +tax_rate_bps : int
  +tax_amount_minor : bigint
  +total_amount_minor : bigint
  +tax_included : bool
  +status : ReportStatus
}

class WarningRule {
  +id : UUID
  +name : str
  +currency : str
  +threshold_minor : bigint
  +severity : Severity
  +message : str
  +active : bool
}
enum Severity {
  INFO
  WARNING
  BLOCKING
}

class ExpenseWarning {
  +id : UUID
  +severity : Severity
  +message : str
  +created_at : datetime
}

class ApprovalStep {
  +id : UUID
  +step_order : int
  +status : StepStatus
  +decided_at : datetime
  +comment : str
  +created_at : datetime
  +updated_at : datetime
}
enum StepStatus {
  PENDING
  APPROVED
  REJECTED
  SKIPPED
}

class ApprovalEvent {
  +id : UUID
  +action : Action
  +comment : str
  +created_at : datetime
}
enum Action {
  SUBMITTED
  APPROVED
  REJECTED
  COMMENTED
  RETURNED_TO_DRAFT
  PAID
}

TimeStampedModel <|-- User
TimeStampedModel <|-- ExpenseCategory
TimeStampedModel <|-- ExpenseReport
TimeStampedModel <|-- Expense
TimeStampedModel <|-- WarningRule
TimeStampedModel <|-- ApprovalStep

User ..> Role
ExpenseReport ..> ReportStatus
Expense ..> ReportStatus
WarningRule ..> Severity
ExpenseWarning ..> Severity
ApprovalStep ..> StepStatus
ApprovalEvent ..> Action

User "1" --> "0..*" ExpenseReport : owns (PROTECT)
User "1" --> "0..*" Expense : incurs (PROTECT)
ExpenseReport "1" --> "1..*" Expense : contains (CASCADE)
ExpenseCategory "0..1" --> "0..*" Expense : categorizes (PROTECT)
ExpenseCategory "0..1" --> "0..*" WarningRule : scopes (PROTECT)
Expense "1" --> "0..*" ExpenseWarning : raises (CASCADE)
WarningRule "0..1" --> "0..*" ExpenseWarning : snapshot of (SET_NULL)
ExpenseReport "1" --> "0..*" ApprovalStep : has (CASCADE)
User "1" --> "0..*" ApprovalStep : approves (PROTECT)
ExpenseReport "1" --> "0..*" ApprovalEvent : audited by (CASCADE)
User "1" --> "0..*" ApprovalEvent : acts on (PROTECT)

@enduml
```
