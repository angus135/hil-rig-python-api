import pytest

from hilrig import (
    SPIBaud,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)
from hilrig import Test as HilRigTest
from hilrig.exceptions import ConfigurationError, PeripheralError
from hilrig.models.configuration import SPIConfiguration, UARTConfiguration
from hilrig.models.instructions import SPITransferInstruction, UARTWriteInstruction


def test_supported_spi_baud_enumeration_is_complete() -> None:
    assert {baud.value for baud in SPIBaud} == {
        45_000_000,
        22_500_000,
        11_250_000,
        5_625_000,
        2_813_000,
        1_406_000,
        703_000,
        352_000,
    }


def test_spi_configuration_is_stored() -> None:
    test = HilRigTest(name="SPI configuration")
    spi = test.spi(channel=0)

    spi.configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_45MBIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )

    assert test.configuration.for_channel(spi.identity) == SPIConfiguration(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_45MBIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )


@pytest.mark.parametrize(
    ("tx_data", "rx_length"),
    [
        (b"\x01\x55", 0),
        (b"\xaa\xbb", 2),
        (b"", 4),
        (b"\x80", 2),
    ],
)
def test_spi_transfer_supports_write_duplex_read_and_command_read(
    tx_data: bytes,
    rx_length: int,
) -> None:
    test = HilRigTest(name="SPI transfer")
    spi = test.spi(channel=1)
    spi.configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_11M25BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_3,
        first_bit=SPIFirst.LSB,
    )

    spi.transfer(tx_data=tx_data, rx_length=rx_length, at_ms=250)

    instruction = tuple(test.instructions)[0]
    assert isinstance(instruction, SPITransferInstruction)
    assert instruction.instruction_id == 0
    assert instruction.timestamp == 250
    assert instruction.tx_data == tx_data
    assert instruction.rx_length == rx_length


def test_spi_transfer_requires_a_configured_master_channel() -> None:
    unconfigured_test = HilRigTest(name="Unconfigured SPI")

    with pytest.raises(ConfigurationError, match="must be configured"):
        unconfigured_test.spi(channel=0).transfer(
            tx_data=b"\x00",
            rx_length=0,
            at_tick=0,
        )

    slave_test = HilRigTest(name="SPI slave")
    slave = slave_test.spi(channel=0)
    slave.configure(
        role=SPIRole.SLAVE,
        baud=SPIBaud.BAUD_352KBIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_1,
        first_bit=SPIFirst.MSB,
    )

    with pytest.raises(PeripheralError, match="only defined for a master"):
        slave.transfer(tx_data=b"\x00", rx_length=0, at_tick=0)


@pytest.mark.parametrize(
    ("tx_data", "rx_length", "message"),
    [
        (b"\x01", 0, "tx_data length must be even"),
        (b"\x01\x02", 1, "rx_length must be even"),
    ],
)
def test_16_bit_spi_transfer_requires_whole_frames(
    tx_data: bytes,
    rx_length: int,
    message: str,
) -> None:
    test = HilRigTest(name="16-bit SPI")
    spi = test.spi(channel=0)
    spi.configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_1M406BIT,
        data_size=SPISize.SIZE_16BIT,
        mode=SPIMode.MODE_2,
        first_bit=SPIFirst.MSB,
    )

    with pytest.raises(ValueError, match=message):
        spi.transfer(tx_data=tx_data, rx_length=rx_length, at_tick=0)


def test_spi_transfer_rejects_no_transmit_and_no_receive() -> None:
    test = HilRigTest(name="Empty SPI")
    spi = test.spi(channel=0)
    spi.configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_703KBIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )

    with pytest.raises(ValueError, match="at least one byte"):
        spi.transfer(tx_data=b"", rx_length=0, at_tick=0)


def test_uart_configuration_is_stored() -> None:
    test = HilRigTest(name="UART configuration")
    uart = test.uart(channel=0)

    uart.configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        parity=UARTParity.ODD,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.TWO,
    )

    assert test.configuration.for_channel(uart.identity) == UARTConfiguration(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        parity=UARTParity.ODD,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.TWO,
    )


@pytest.mark.parametrize("baud_hz", [0, -1, 921_601, 115_200.0, True])
def test_uart_rejects_invalid_baud_rates(baud_hz) -> None:
    test = HilRigTest(name="Invalid UART baud")

    with pytest.raises(ValueError, match="baud_hz"):
        test.uart(channel=0).configure(
            mode=UARTMode.RS232,
            baud_hz=baud_hz,
            parity=UARTParity.NONE,
            length=UARTLengthBits.NINE,
            stop=UARTStopBits.ONE,
        )


def test_uart_write_stores_raw_bytes() -> None:
    test = HilRigTest(name="UART bytes")

    test.uart(channel=0).write(data=b"START\r\n", at_ms=100)

    instruction = tuple(test.instructions)[0]
    assert isinstance(instruction, UARTWriteInstruction)
    assert instruction.timestamp == 100
    assert instruction.data == b"START\r\n"


def test_uart_write_text_encodes_and_stores_bytes() -> None:
    test = HilRigTest(name="UART text")

    test.uart(channel=0).write_text(data="START\r\n", encoding="ascii", at_tick=100)

    instruction = tuple(test.instructions)[0]
    assert isinstance(instruction, UARTWriteInstruction)
    assert instruction.data == b"START\r\n"


def test_uart_write_text_rejects_unknown_encoding() -> None:
    test = HilRigTest(name="UART encoding")

    with pytest.raises(ValueError, match="Unknown text encoding"):
        test.uart(channel=0).write_text(
            data="hello",
            encoding="not-a-real-codec",
            at_tick=0,
        )
