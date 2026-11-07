from typing import Optional
import datetime
import decimal

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Identity, Index, Integer, Numeric, PrimaryKeyConstraint, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class Awb(Base):
    __tablename__ = 'awb'
    __table_args__ = (
        PrimaryKeyConstraint('awb_no', name='awb_pk'),
    )

    awb_no: Mapped[str] = mapped_column(String(12), primary_key=True)
    priority_score: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(4, 2))
    origin: Mapped[Optional[str]] = mapped_column(String(30))
    dest: Mapped[Optional[str]] = mapped_column(String(30))
    issued_date: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    contract_ddl: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    declared_value: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(15, 2))
    ttl_piece: Mapped[Optional[int]] = mapped_column(Integer)

    shipment: Mapped[list['Shipment']] = relationship('Shipment', back_populates='awb')
    awb_uld_flight: Mapped[list['AwbUldFlight']] = relationship('AwbUldFlight', back_populates='awb')


class Customer(Base):
    __tablename__ = 'customer'
    __table_args__ = (
        PrimaryKeyConstraint('customer_id', name='customer_pk'),
        UniqueConstraint('account_number', name='customer_account_number_key')
    )

    customer_id: Mapped[int] = mapped_column(Integer, Identity(always=True, start=1, increment=1, minvalue=1, maxvalue=2147483647, cycle=False, cache=1), primary_key=True, autoincrement=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(50))
    comp_name: Mapped[Optional[str]] = mapped_column(String(50))
    contact_person: Mapped[Optional[str]] = mapped_column(String(50))
    email: Mapped[Optional[str]] = mapped_column(String(100))
    phone_no: Mapped[Optional[str]] = mapped_column(String(30))

    shipment_consignee: Mapped[list['Shipment']] = relationship('Shipment', foreign_keys='[Shipment.consignee_id]', back_populates='consignee')
    shipment_shipper: Mapped[list['Shipment']] = relationship('Shipment', foreign_keys='[Shipment.shipper_id]', back_populates='shipper')


class Deck(Base):
    __tablename__ = 'deck'
    __table_args__ = (
        PrimaryKeyConstraint('deck_code', name='deck_pk'),
    )

    deck_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    plan: Mapped[Optional[object]] = mapped_column(JSONB, comment='JSONB representation of aircraft deck layout (Left/Right separated)')

    aircraft: Mapped[list['Aircraft']] = relationship('Aircraft', back_populates='deck')


class Loadtype(Base):
    __tablename__ = 'loadtype'
    __table_args__ = (
        PrimaryKeyConstraint('loadtype_id', name='loadtype_pk'),
    )

    loadtype_id: Mapped[int] = mapped_column(Integer, Identity(always=True, start=1, increment=1, minvalue=1, maxvalue=2147483647, cycle=False, cache=1), primary_key=True, autoincrement=True)
    l_type: Mapped[Optional[str]] = mapped_column(String(3))
    description: Mapped[Optional[str]] = mapped_column(String(2000))
    max_weight_kg: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    tare_weight_kg: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(5, 2))
    capacity_cbm: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(5, 2))
    main_deck: Mapped[Optional[bool]] = mapped_column(Boolean)
    lower_deck: Mapped[Optional[bool]] = mapped_column(Boolean)
    allowed_aircraft: Mapped[Optional[str]] = mapped_column(String(50))

    uld: Mapped[list['Uld']] = relationship('Uld', back_populates='loadtype_loadtype')


class Aircraft(Base):
    __tablename__ = 'aircraft'
    __table_args__ = (
        ForeignKeyConstraint(['deck_deck_code'], ['deck.deck_code'], name='aircraft_deck_fk'),
        PrimaryKeyConstraint('aircraft_id', name='aircraft_pk')
    )

    aircraft_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    deck_deck_code: Mapped[str] = mapped_column(String(10), nullable=False)
    is_pax: Mapped[Optional[str]] = mapped_column(String(1))
    max_payload_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(20, 2))
    aircraft_type: Mapped[Optional[str]] = mapped_column(String(10))
    max_zero_fuel_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    max_takeoff_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    max_landing_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))

    deck: Mapped['Deck'] = relationship('Deck', back_populates='aircraft')
    flight: Mapped[list['Flight']] = relationship('Flight', back_populates='aircraft_aircraft')


class Shipment(Base):
    __tablename__ = 'shipment'
    __table_args__ = (
        ForeignKeyConstraint(['awb_awb_no'], ['awb.awb_no'], name='shipment_awb_fk'),
        ForeignKeyConstraint(['consignee_id'], ['customer.customer_id'], name='shipment_customer_consignee_fk'),
        ForeignKeyConstraint(['shipper_id'], ['customer.customer_id'], name='shipment_customer_shipper_fk'),
        PrimaryKeyConstraint('s_no', name='shipment_pk'),
        Index('shipment_idx', 'awb_awb_no', unique=True)
    )

    s_no: Mapped[str] = mapped_column(String(50), primary_key=True)
    awb_awb_no: Mapped[str] = mapped_column(String(12), nullable=False)
    shipper_id: Mapped[int] = mapped_column(Integer, nullable=False)
    consignee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    commodity_type: Mapped[Optional[str]] = mapped_column(String(15))
    ttl_weight_kg: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    ttl_volume_cbm: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    length: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    width: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    height: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    special_handling_code: Mapped[Optional[str]] = mapped_column(String(10))

    awb: Mapped['Awb'] = relationship('Awb', back_populates='shipment')
    consignee: Mapped['Customer'] = relationship('Customer', foreign_keys=[consignee_id], back_populates='shipment_consignee')
    shipper: Mapped['Customer'] = relationship('Customer', foreign_keys=[shipper_id], back_populates='shipment_shipper')


class Uld(Base):
    __tablename__ = 'uld'
    __table_args__ = (
        ForeignKeyConstraint(['loadtype_loadtype_id'], ['loadtype.loadtype_id'], name='uld_loadtype_fk'),
        PrimaryKeyConstraint('uld_no', name='uld_pk')
    )

    uld_no: Mapped[str] = mapped_column(String(12), primary_key=True)
    loadtype_loadtype_id: Mapped[int] = mapped_column(Integer, nullable=False)
    current_location: Mapped[Optional[str]] = mapped_column(String(50))
    condition: Mapped[Optional[str]] = mapped_column(String(30))
    last_scanned: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)

    loadtype_loadtype: Mapped['Loadtype'] = relationship('Loadtype', back_populates='uld')
    awb_uld_flight: Mapped[list['AwbUldFlight']] = relationship('AwbUldFlight', back_populates='uld')
    uld_flight: Mapped[list['UldFlight']] = relationship('UldFlight', back_populates='uld')


class Flight(Base):
    __tablename__ = 'flight'
    __table_args__ = (
        ForeignKeyConstraint(['aircraft_aircraft_id'], ['aircraft.aircraft_id'], name='flight_aircraft_fk'),
        PrimaryKeyConstraint('flight_id', name='flight_pk')
    )

    flight_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    aircraft_aircraft_id: Mapped[str] = mapped_column(String(10), nullable=False)
    flight_no: Mapped[Optional[str]] = mapped_column(String(10))
    origin: Mapped[Optional[str]] = mapped_column(String(30))
    dest: Mapped[Optional[str]] = mapped_column(String(30))
    std: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, comment='Scheduled Departure')
    etd: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, comment='Estimated Departure')
    atd: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, comment='Actual Departure')
    status: Mapped[Optional[str]] = mapped_column(String(50))
    planned_fuel_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    actual_fuel_weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    planned_zfw: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    actual_zfw: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    planned_ldw: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))
    planned_tow: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(10, 2))

    aircraft_aircraft: Mapped['Aircraft'] = relationship('Aircraft', back_populates='flight')
    awb_uld_flight: Mapped[list['AwbUldFlight']] = relationship('AwbUldFlight', back_populates='flight_flight')
    uld_flight: Mapped[list['UldFlight']] = relationship('UldFlight', back_populates='flight_flight')


class AwbUldFlight(Base):
    __tablename__ = 'awb_uld_flight'
    __table_args__ = (
        ForeignKeyConstraint(['awb_awb_no'], ['awb.awb_no'], name='awb_uld_awb_fk'),
        ForeignKeyConstraint(['flight_flight_id'], ['flight.flight_id'], name='awb_uld_flight_fk'),
        ForeignKeyConstraint(['uld_uld_no'], ['uld.uld_no'], name='awb_uld_uld_fk'),
        PrimaryKeyConstraint('awb_awb_no', 'uld_uld_no', 'flight_flight_id', name='awb_uld_pk')
    )

    awb_awb_no: Mapped[str] = mapped_column(String(12), primary_key=True)
    uld_uld_no: Mapped[str] = mapped_column(String(12), primary_key=True)
    flight_flight_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    weight: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(7, 2))
    volume: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(15, 2))
    status: Mapped[Optional[str]] = mapped_column(String(30))
    piece_count: Mapped[Optional[int]] = mapped_column(Integer)
    location: Mapped[Optional[str]] = mapped_column(String(20))
    recommended_offload: Mapped[Optional[bool]] = mapped_column(Boolean, server_default=text('false'))
    offload_probability: Mapped[Optional[decimal.Decimal]] = mapped_column(Numeric(3, 2))
    risk_level: Mapped[Optional[str]] = mapped_column(String(20))
    risk_types: Mapped[Optional[object]] = mapped_column(JSONB)
    risk_reasons: Mapped[Optional[object]] = mapped_column(JSONB)

    awb: Mapped['Awb'] = relationship('Awb', back_populates='awb_uld_flight')
    flight_flight: Mapped['Flight'] = relationship('Flight', back_populates='awb_uld_flight')
    uld: Mapped['Uld'] = relationship('Uld', back_populates='awb_uld_flight')


class UldFlight(Base):
    __tablename__ = 'uld_flight'
    __table_args__ = (
        ForeignKeyConstraint(['flight_flight_id'], ['flight.flight_id'], name='uld_flight_flight_fk'),
        ForeignKeyConstraint(['uld_uld_no'], ['uld.uld_no'], name='uld_flight_uld_fk'),
        PrimaryKeyConstraint('uld_uld_no', 'flight_flight_id', name='uld_flight_pk')
    )

    uld_uld_no: Mapped[str] = mapped_column(String(12), primary_key=True)
    flight_flight_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[Optional[str]] = mapped_column(String(30))
    position: Mapped[Optional[str]] = mapped_column(String(10))
    contour: Mapped[Optional[str]] = mapped_column(String(10))

    flight_flight: Mapped['Flight'] = relationship('Flight', back_populates='uld_flight')
    uld: Mapped['Uld'] = relationship('Uld', back_populates='uld_flight')
