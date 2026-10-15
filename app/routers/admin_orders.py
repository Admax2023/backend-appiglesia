"""Admin: orders, payments and payment proofs."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.deps import AdminUser, DbSession, OptionalIdemKey, Pagination, RequiredIdemKey, client_ip, require_admin
from app.enums import CreditStatus, OrderStatus, PaymentMethod, PaymentRecordStatus, PaymentStatus, ProofStatus
from app.routers.common import ERR, DateFrom, DateTo, optional_range
from app.schemas.common import Page
from app.schemas.orders import (
    AdminOrderCreate,
    AdminOrderCreatedOut,
    ApproveProofIn,
    CancelIn,
    CreditAuthorizeIn,
    CreditRejectIn,
    LinkCustomerIn,
    OrderDetailOut,
    OrderSummaryOut,
    PaymentOut,
    PaymentResultOut,
    ProofOut,
    ProofReviewOut,
    RefundIn,
    RegisterPaymentIn,
    RejectProofIn,
    StatusChangeIn,
)
from app.services import idempotency, storage
from app.services import orders as orders_svc
from app.services import payments as payments_svc

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)], responses=ERR)


# --------------------------------------------------------------------------- orders


@router.get("/orders", response_model=Page[OrderSummaryOut], tags=["admin-orders"])
def list_orders(
    db: DbSession,
    pg: Pagination,
    status: Annotated[list[OrderStatus] | None, Query(description="Repetible: ?status=confirmed&status=ready")] = None,
    payment_status: Annotated[list[PaymentStatus] | None, Query()] = None,
    payment_method: PaymentMethod | None = None,
    credit_status: CreditStatus | None = None,
    customer_id: uuid.UUID | None = None,
    has_pending_proofs: bool | None = None,
    search: Annotated[str | None, Query(max_length=100, description="Código, nombre o teléfono")] = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> Page[OrderSummaryOut]:
    start, end = optional_range(db, date_from, date_to)
    items, total = orders_svc.list_orders(
        db, offset=pg.offset, limit=pg.page_size, status=status, payment_status=payment_status,
        payment_method=payment_method, credit_status=credit_status, customer_id=customer_id, start=start, end=end,
        search=search, has_pending_proofs=has_pending_proofs,
    )
    return Page[OrderSummaryOut].build(items, total, pg.page, pg.page_size)


@router.post("/orders", status_code=201, response_model=AdminOrderCreatedOut, tags=["admin-orders"])
def create_order(
    body: AdminOrderCreate, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, AdminOrderCreatedOut]:
        order, token = orders_svc.create_order(db, body, actor=user, ip=ip)
        return 201, AdminOrderCreatedOut(tracking_token=token, order=orders_svc.detail(db, order))

    return idempotency.run(
        db, scope=f"admin:create_order:{user.id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op,
        secret_fields=("tracking_token",),
    )


@router.get("/orders/{order_id}", response_model=OrderDetailOut, tags=["admin-orders"])
def get_order(order_id: uuid.UUID, db: DbSession) -> OrderDetailOut:
    return orders_svc.detail(db, orders_svc.get_order(db, order_id))


@router.post("/orders/{order_id}/status", response_model=OrderDetailOut, tags=["admin-orders"])
def change_status(
    order_id: uuid.UUID, body: StatusChangeIn, request: Request, db: DbSession, user: AdminUser
) -> OrderDetailOut:
    order = orders_svc.change_status(db, order_id, OrderStatus(body.status), body.note, user, client_ip(request))
    out = orders_svc.detail(db, order)
    db.commit()
    return out


@router.post("/orders/{order_id}/cancel", response_model=OrderDetailOut, tags=["admin-orders"])
def cancel_order(order_id: uuid.UUID, body: CancelIn, request: Request, db: DbSession, user: AdminUser) -> OrderDetailOut:
    order = orders_svc.cancel(db, order_id, body.reason, user, client_ip(request))
    out = orders_svc.detail(db, order)
    db.commit()
    return out


@router.post("/orders/{order_id}/credit/authorize", response_model=OrderDetailOut, tags=["admin-orders"])
def authorize_credit(
    order_id: uuid.UUID, body: CreditAuthorizeIn, request: Request, db: DbSession, user: AdminUser
) -> OrderDetailOut:
    order = orders_svc.authorize_credit(db, order_id, body.note, user, client_ip(request))
    out = orders_svc.detail(db, order)
    db.commit()
    return out


@router.post("/orders/{order_id}/credit/reject", response_model=OrderDetailOut, tags=["admin-orders"])
def reject_credit(
    order_id: uuid.UUID, body: CreditRejectIn, request: Request, db: DbSession, user: AdminUser
) -> OrderDetailOut:
    order = orders_svc.reject_credit(db, order_id, body.reason, user, client_ip(request))
    out = orders_svc.detail(db, order)
    db.commit()
    return out


@router.post("/orders/{order_id}/customer", response_model=OrderDetailOut, tags=["admin-orders"])
def link_customer(
    order_id: uuid.UUID, body: LinkCustomerIn, request: Request, db: DbSession, user: AdminUser
) -> OrderDetailOut:
    """Vincula el pedido (y su deuda) a otro cliente. Única forma de asociar un pedido web a un cliente
    existente: la API nunca lo hace sola por coincidencia de teléfono."""
    order = orders_svc.link_customer(db, order_id, body.customer_id, body.note, user, client_ip(request))
    out = orders_svc.detail(db, order)
    db.commit()
    return out


@router.post("/orders/{order_id}/payments", status_code=201, response_model=PaymentResultOut, tags=["admin-payments"])
def register_payment(
    order_id: uuid.UUID,
    body: RegisterPaymentIn,
    request: Request,
    db: DbSession,
    user: AdminUser,
    idem_key: RequiredIdemKey,
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, PaymentResultOut]:
        payment, order, movement = payments_svc.register_payment(db, order_id, body, user, ip)
        return 201, PaymentResultOut(
            payment=PaymentOut.model_validate(payment),
            order=orders_svc.summary(db, order),
            cash_movement_id=movement.id if movement else None,
        )

    return idempotency.run(
        db, scope=f"admin:payment:{order_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )


# --------------------------------------------------------------------------- payments


@router.get("/payments", response_model=Page[PaymentOut], tags=["admin-payments"])
def list_payments(
    db: DbSession,
    pg: Pagination,
    method: PaymentMethod | None = None,
    status: PaymentRecordStatus | None = None,
    order_id: uuid.UUID | None = None,
    cash_session_id: uuid.UUID | None = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
) -> Page[PaymentOut]:
    start, end = optional_range(db, date_from, date_to)
    rows, total = payments_svc.list_payments(
        db, offset=pg.offset, limit=pg.page_size, method=method, status=status, order_id=order_id,
        cash_session_id=cash_session_id, start=start, end=end,
    )
    return Page[PaymentOut].build([PaymentOut.model_validate(p) for p in rows], total, pg.page, pg.page_size)


@router.get("/payments/{payment_id}", response_model=PaymentOut, tags=["admin-payments"])
def get_payment(payment_id: uuid.UUID, db: DbSession) -> PaymentOut:
    return PaymentOut.model_validate(payments_svc.get_payment(db, payment_id))


@router.post("/payments/{payment_id}/refund", response_model=PaymentResultOut, tags=["admin-payments"])
def refund_payment(
    payment_id: uuid.UUID, body: RefundIn, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, PaymentResultOut]:
        payment, order, movement = payments_svc.refund_payment(db, payment_id, body.reason, user, ip)
        return 200, PaymentResultOut(
            payment=PaymentOut.model_validate(payment),
            order=orders_svc.summary(db, order),
            cash_movement_id=movement.id if movement else None,
        )

    return idempotency.run(
        db, scope=f"admin:refund:{payment_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )


# --------------------------------------------------------------------------- proofs


@router.get("/proofs", response_model=Page[ProofOut], tags=["admin-proofs"])
def list_proofs(
    db: DbSession, pg: Pagination, status: ProofStatus | None = None, order_id: uuid.UUID | None = None
) -> Page[ProofOut]:
    rows, total = payments_svc.list_proofs(db, offset=pg.offset, limit=pg.page_size, status=status, order_id=order_id)
    return Page[ProofOut].build([ProofOut.model_validate(p) for p in rows], total, pg.page, pg.page_size)


@router.get("/proofs/{proof_id}", response_model=ProofOut, tags=["admin-proofs"])
def get_proof(proof_id: uuid.UUID, db: DbSession) -> ProofOut:
    return ProofOut.model_validate(payments_svc.get_proof(db, proof_id))


@router.get(
    "/proofs/{proof_id}/file",
    tags=["admin-proofs"],
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}},
                     "description": "Imagen del comprobante"}},
)
def get_proof_file(proof_id: uuid.UUID, db: DbSession) -> Response:
    proof = payments_svc.get_proof(db, proof_id)
    data = storage.read_proof(proof.storage_path, proof.mime_type)
    ext = storage.ext_for(proof.mime_type)
    return Response(
        data,
        media_type=proof.mime_type,
        headers={
            "Cache-Control": "no-store, private",
            "Content-Disposition": f'inline; filename="comprobante-{proof.id}.{ext}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/proofs/{proof_id}/approve", response_model=ProofReviewOut, tags=["admin-proofs"])
def approve_proof(
    proof_id: uuid.UUID, body: ApproveProofIn, request: Request, db: DbSession, user: AdminUser,
    idem_key: OptionalIdemKey,
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, ProofReviewOut]:
        proof, payment, order = payments_svc.approve_proof(
            db, proof_id, body.amount, body.note, user, ip, allow_duplicate=body.allow_duplicate
        )
        return 200, ProofReviewOut(
            proof=ProofOut.model_validate(proof), payment=PaymentOut.model_validate(payment),
            order=orders_svc.summary(db, order),
        )

    return idempotency.run(
        db, scope=f"admin:proof_approve:{proof_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )


@router.post("/proofs/{proof_id}/reject", response_model=ProofReviewOut, tags=["admin-proofs"])
def reject_proof(
    proof_id: uuid.UUID, body: RejectProofIn, request: Request, db: DbSession, user: AdminUser
) -> ProofReviewOut:
    proof, order = payments_svc.reject_proof(db, proof_id, body.reason, user, client_ip(request))
    out = ProofReviewOut(proof=ProofOut.model_validate(proof), payment=None, order=orders_svc.summary(db, order))
    db.commit()
    return out


@router.post("/proofs/{proof_id}/refund", response_model=ProofReviewOut, tags=["admin-proofs"])
def refund_proof(
    proof_id: uuid.UUID, body: RefundIn, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, ProofReviewOut]:
        proof, payment, order = payments_svc.refund_proof(db, proof_id, body.reason, user, ip)
        return 200, ProofReviewOut(
            proof=ProofOut.model_validate(proof), payment=PaymentOut.model_validate(payment),
            order=orders_svc.summary(db, order),
        )

    return idempotency.run(
        db, scope=f"admin:proof_refund:{proof_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )
