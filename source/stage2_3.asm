; =============================================================================
; REMOTE_AGENT STAGE 2 — persistent, network-controllable agent on the real
; RTL8168 found on the Gigabyte B450M DS3H testbench (bus=004 dev=000
; func=000, confirmed by netprobe.img's enumeration: vendor:device=
; 10EC:8168, BAR2/3 = 64-bit MMIO at 0xFB500000 non-prefetchable, BAR4/5 =
; 64-bit MMIO at 0xEC000000 prefetchable, BAR0 = I/O at 0xF000).
;
; Built on nic_probe_1-7's proven groundwork: BAR2 holds real register
; content (round 1); software reset completes instantly (round 2); TX
; verified byte-for-byte in a real Wireshark capture (round 3); RX finally
; worked once MISC's RXDV_GATED_EN bit (0xF0, bit 19 -- gates the
; RX-data-valid signal at the PHY/MAC boundary itself) was cleared (round
; 7, after rounds 4-6 tried a correctly-sized ring/buffer and still never
; received a single frame). A full two-way PING/echo exchange was
; confirmed on real hardware immediately after that fix.
;
; This round turns the one-shot RX-then-halt test into remote_loop: a
; persistent command loop that never returns, answering PING/READ32/
; WRITE32 commands over the network indefinitely -- see remote_loop below
; for the wire protocol. The point: no more reboots needed to keep
; working. Further GPU register-mapping work and anything else this
; project needs can now be driven live, over this link, from Windows.
;
; WHY THIS EXISTS: the reference hdgl_nic.asm driver's "RTL" code path is
; written for the RTL8139 legacy-ring register interface (CAPR/CBR/TSAD0/
; RBSTART, I/O-port programmed) -- that's pre-2003 Realtek silicon. RTL8168
; uses Realtek's newer "C+" descriptor-ring architecture instead, a
; different register set entirely, much closer to how e1000 works than to
; RTL8139. QEMU's default NIC model IS rtl8139, so that mismatch never shows
; up under emulation -- only on real hardware, which is the most likely
; explanation for the earlier NIC work not booting on metal.
;
; Register offsets below are cross-checked against the Linux r8169 driver
; source (drivers/net/ethernet/realtek/r8169_main.c): ChipCmd=0x37,
; TxConfig=0x40, RxConfig=0x44, Cfg9346=0x50, Config1-5=0x52-0x56,
; PHYAR=0x60, PHYstatus=0x6c, IntrMask=0x3c, IntrStatus=0x3e -- all matched
; exactly what this project's own reading already used. ChipCmdBits:
; CmdReset=0x10 (self-clears on completion), CmdRxEnb=0x08, CmdTxEnb=0x04 --
; the same bit across the whole RTL8139/8169/8168 line, confirmed from the
; same source. TX/RX descriptors in C+ mode are {opts1:u32, opts2:u32,
; addr:u64} = 16 bytes, DescOwn=bit31, RingEnd=bit30, FirstFrag=bit29,
; LastFrag=bit28 -- not used yet in this round, recorded for the round that
; actually builds TX/RX rings.
;
; The reset is issued, polled to completion with a bounded timeout (so a
; chip that never clears the bit can't hang this forever), and the full
; register set is dumped both BEFORE and AFTER so the two can be compared
; directly. No ring setup, no TX/RX enable, no interrupt config yet --
; staying incremental on purpose, one verified step at a time.
;
; If BAR3 (the 64-bit BAR's upper dword) turns out non-zero, this probe
; says so and stops rather than silently misaddressing memory this 32-bit
; flat protected-mode environment (no paging, no PAE) cannot actually reach.
; =============================================================================

BITS 16
ORG 0x8000

%ifndef TARGET_VENDOR
%define TARGET_VENDOR 0x10EC
%endif
%ifndef TARGET_DEVICE
%define TARGET_DEVICE 0x8168
%endif

stage2_start:
    mov si, msg_stage2
    call print_str16

    in al, 0x92
    or al, 2
    out 0x92, al

    cli
    lgdt [gdt_descriptor]

    mov eax, cr0
    or eax, 1
    mov cr0, eax

    jmp CODE_SEG:pm_start

print_str16:
    lodsb
    or al, al
    jz .done
    mov ah, 0x0E
    mov bh, 0
    int 0x10
    jmp print_str16
.done:
    ret

msg_stage2: db 'Stage2 loaded. Entering protected mode...', 13, 10, 0

gdt_start:
gdt_null:  dq 0
gdt_code:
    dw 0xFFFF, 0x0000
    db 0x00, 10011010b, 11001111b, 0x00
gdt_data:
    dw 0xFFFF, 0x0000
    db 0x00, 10010010b, 11001111b, 0x00
gdt_code16:
    dw 0xFFFF, 0x0000
    db 0x00, 10011010b, 0x00, 0x00
gdt_data16:
    dw 0xFFFF, 0x0000
    db 0x00, 10010010b, 0x00, 0x00
gdt_end:

gdt_descriptor:
    dw gdt_end - gdt_start - 1
    dd gdt_start

CODE_SEG   equ gdt_code   - gdt_start
DATA_SEG   equ gdt_data   - gdt_start
CODE16_SEG equ gdt_code16 - gdt_start
DATA16_SEG equ gdt_data16 - gdt_start

BITS 32

pm_start:
    mov ax, DATA_SEG
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ss, ax
    mov esp, 0x90000

    call serial_init
    call clear_screen
    call log_init

    mov esi, pm_banner
    call print32

    call find_and_dump_nic

    call save_log_and_halt      ; never returns

halt_forever:
    hlt
    jmp halt_forever

save_log_and_halt:
    jmp CODE16_SEG:pm16_entry

BITS 16
pm16_entry:
    mov ax, DATA16_SEG
    mov ds, ax
    mov es, ax
    mov fs, ax
    mov gs, ax
    mov ss, ax

    mov eax, cr0
    and eax, 0xFFFFFFFE
    mov cr0, eax

    jmp 0x0000:real_mode_entry

real_mode_entry:
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7C00
    sti

    mov si, log_dap
    mov ah, 0x43
    xor al, al
    mov dl, [0x7000]
    int 0x13
    jnc .write_ok

.write_ok:
    cli
.hang:
    hlt
    jmp .hang

log_dap:
    db 0x10
    db 0
    dw LOG_SECTORS
    dw 0x0000
    dw LOG_SEG
    dd 64
    dd 0

BITS 32

LOG_LINEAR  equ 0x00020000
LOG_SEG     equ 0x2000
LOG_SECTORS equ 64
LOG_BYTES   equ LOG_SECTORS * 512

log_len: dd 0

log_init:
    pusha
    mov edi, LOG_LINEAR
    mov ecx, LOG_BYTES / 4
    xor eax, eax
    rep stosd
    mov dword [log_len], 0
    popa
    ret

log_append_char:
    pusha
    mov edi, LOG_LINEAR
    add edi, [log_len]
    cmp dword [log_len], LOG_BYTES - 1
    jge .full
    mov [edi], al
    inc dword [log_len]
.full:
    popa
    ret

VGA_MEM  equ 0xB8000
VGA_COLS equ 80
VGA_ROWS equ 25
VGA_ATTR equ 0x0F

COM1_PORT equ 0x3F8

cursor_pos: dd 0

serial_init:
    pusha
    mov dx, COM1_PORT + 1
    xor al, al
    out dx, al

    mov dx, COM1_PORT + 3
    mov al, 0x80
    out dx, al

    mov dx, COM1_PORT + 0
    mov al, 3
    out dx, al
    mov dx, COM1_PORT + 1
    xor al, al
    out dx, al

    mov dx, COM1_PORT + 3
    mov al, 0x03
    out dx, al

    mov dx, COM1_PORT + 2
    mov al, 0xC7
    out dx, al

    mov dx, COM1_PORT + 4
    mov al, 0x0B
    out dx, al
    popa
    ret

serial_putchar:
    push eax
    push ebx
    push edx
    mov bl, al
.wait:
    mov dx, COM1_PORT + 5
    in al, dx
    test al, 0x20
    jz .wait
    mov dx, COM1_PORT
    mov al, bl
    out dx, al
    pop edx
    pop ebx
    pop eax
    ret

clear_screen:
    pusha
    mov edi, VGA_MEM
    mov ecx, VGA_COLS*VGA_ROWS
    mov ax, (VGA_ATTR << 8) | ' '
.loop:
    mov [edi], ax
    add edi, 2
    loop .loop
    mov dword [cursor_pos], 0
    popa
    ret

print32:
    pusha
.next:
    mov al, [esi]
    inc esi
    cmp al, 0
    je .done
    cmp al, 10
    je .nl
    call emit_char
    jmp .next
.nl:
    call emit_newline
    jmp .next
.done:
    popa
    ret

emit_char:
    pusha
    movzx ebx, al
    call serial_putchar_wrap
    call log_append_char

    mov ebx, [cursor_pos]
    cmp ebx, VGA_COLS*VGA_ROWS
    jl .fits
    call scroll_and_clamp
    mov ebx, [cursor_pos]
.fits:
    mov edi, VGA_MEM
    mov ecx, ebx
    shl ecx, 1
    add edi, ecx
    mov ah, VGA_ATTR
    mov [edi], ax
    inc ebx
    mov [cursor_pos], ebx
    popa
    ret

serial_putchar_wrap:
    push eax
    mov eax, ebx
    call serial_putchar
    pop eax
    ret

emit_newline:
    pusha
    mov al, 13
    call serial_putchar
    call log_append_char
    mov al, 10
    call serial_putchar
    call log_append_char

    mov eax, [cursor_pos]
    xor edx, edx
    mov ecx, VGA_COLS
    div ecx
    inc eax
    mul ecx
    mov [cursor_pos], eax
    cmp eax, VGA_COLS*VGA_ROWS
    jl .fits
    call scroll_and_clamp
.fits:
    popa
    ret

scroll_and_clamp:
    pusha
    call clear_screen
    popa
    ret

print_hex8:
    pusha
    mov bl, al
    shr al, 4
    call .nib
    mov al, bl
    and al, 0x0F
    call .nib
    popa
    ret
.nib:
    cmp al, 10
    jl .d
    add al, 'A'-10
    jmp .o
.d:
    add al, '0'
.o:
    call emit_char
    ret

print_hex16:
    pusha
    mov ecx, 4
.loop:
    rol ax, 4
    push eax
    and al, 0x0F
    cmp al, 10
    jl .d
    add al, 'A'-10
    jmp .o
.d:
    add al, '0'
.o:
    call emit_char
    pop eax
    loop .loop
    popa
    ret

print_hex32:
    pusha
    mov ecx, 8
.loop:
    rol eax, 4
    push eax
    and al, 0x0F
    cmp al, 10
    jl .d
    add al, 'A'-10
    jmp .o
.d:
    add al, '0'
.o:
    call emit_char
    pop eax
    loop .loop
    popa
    ret

; -----------------------------------------------------------------------------
; PCI config space access.
; -----------------------------------------------------------------------------
PCI_CONFIG_ADDR equ 0x0CF8
PCI_CONFIG_DATA equ 0x0CFC

; EBX=bus, ECX=device, EDX=function, ESI=register -> EAX=addr
build_pci_addr:
    mov eax, ebx
    shl eax, 16
    mov ebp, ecx
    shl ebp, 11
    or eax, ebp
    mov ebp, edx
    shl ebp, 8
    or eax, ebp
    or eax, esi
    or eax, 0x80000000
    ret

; EBX=bus, ECX=device, EDX=function, ESI=register -> EAX=dword read
pci_read32:
    call build_pci_addr
    push edx
    mov dx, PCI_CONFIG_ADDR
    out dx, eax
    mov dx, PCI_CONFIG_DATA
    in eax, dx
    pop edx
    ret

; =============================================================================
; Find TARGET_VENDOR:TARGET_DEVICE, decode its BAR2 (64-bit MMIO), and dump
; the RTL8139/8169/8168-family-stable register set. Entirely read-only.
; =============================================================================
find_and_dump_nic:
    pusha

    mov esi, scanning_msg
    call print32

    xor ebx, ebx
.bus_loop:
    xor ecx, ecx
.dev_loop:
    xor edx, edx
.func_loop:
    push esi
    xor esi, esi
    call pci_read32
    pop esi

    cmp ax, 0xFFFF
    je .next_func

    mov edi, eax
    and edi, 0x0000FFFF
    cmp edi, TARGET_VENDOR
    jne .next_func
    mov edi, eax
    shr edi, 16
    cmp edi, TARGET_DEVICE
    jne .next_func

    call dump_nic_registers
    jmp .done

.next_func:
    inc edx
    cmp edx, 8
    jl .func_loop
    inc ecx
    cmp ecx, 32
    jl .dev_loop
    inc ebx
    cmp ebx, 256
    jl .bus_loop

    mov esi, not_found_msg
    call print32
    jmp .done

.done:
    popa
    ret

; EBX=bus, ECX=device, EDX=function of the matched NIC.
dump_nic_registers:
    pusha

    mov esi, found_msg
    call print32
    push ebx
    push ecx
    push edx

    ; BAR2 at config offset 0x18
    mov esi, 0x18
    call pci_read32
    mov [bar2_raw], eax

    mov esi, bar2_label
    call print32
    mov eax, [bar2_raw]
    call print_hex32
    call emit_newline

    ; Type check: bits[2:1] of the low dword. 64-bit BAR = 0b10.
    mov eax, [bar2_raw]
    and eax, 0x6
    cmp eax, 0x4
    jne .bar2_not_64bit

    ; BAR3 at config offset 0x1C (upper 32 bits)
    pop edx
    pop ecx
    pop ebx
    push ebx
    push ecx
    push edx
    mov esi, 0x1C
    call pci_read32
    mov [bar3_raw], eax

    mov esi, bar3_label
    call print32
    mov eax, [bar3_raw]
    call print_hex32
    call emit_newline

    cmp dword [bar3_raw], 0
    je .bar_addr_ok

    mov esi, bar3_nonzero_msg
    call print32
    jmp nic_dump_exit

.bar2_not_64bit:
.bar_addr_ok:
    mov eax, [bar2_raw]
    and eax, 0xFFFFFFF0
    mov [mmio_base], eax

    mov esi, mmio_base_label
    call print32
    mov eax, [mmio_base]
    call print_hex32
    call emit_newline
    call emit_newline

    mov esi, before_reset_hdr
    call print32
    call dump_regs

    call do_reset

    mov esi, after_reset_hdr
    call print32
    call dump_regs

    call disable_rxdvgate

    call tx_test

    ; remote_loop never returns -- once started, this boot image stays a
    ; persistent, network-controllable agent. No more reboots needed
    ; after this one; further work happens by sending it commands, not by
    ; flashing a new image.
    call remote_loop

    jmp nic_dump_exit

; Reusable register dump -- reads [mmio_base], prints MAC/CR/IMR/ISR/TCR/
; RCR/Cfg9346/Config1-5/PHYAR/PHYstatus. Called twice: once before the
; reset, once after, so the two dumps can be compared directly.
dump_regs:
    pusha

    ; ── MAC address: IDR0-IDR5, offsets 0x00-0x05 (byte-wide) ──
    mov esi, mac_label
    call print32
    mov ebp, [mmio_base]
    xor edi, edi
.mac_loop:
    movzx eax, byte [ebp + edi]
    call print_hex8
    inc edi
    cmp edi, 6
    jge .mac_done
    mov al, ':'
    call emit_char
    jmp .mac_loop
.mac_done:
    call emit_newline

    ; ── CR (0x37), 1 byte ──
    mov esi, cr_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, byte [ebp + 0x37]
    call print_hex8
    call emit_newline

    ; ── IMR (0x3C), ISR (0x3E), 2 bytes each ──
    mov esi, imr_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, word [ebp + 0x3C]
    call print_hex16
    call emit_newline

    mov esi, isr_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, word [ebp + 0x3E]
    call print_hex16
    call emit_newline

    ; ── TCR (0x40), RCR (0x44), 4 bytes each -- TCR's high bits carry the
    ;    hardware version ID (exact RTL8168 sub-revision); decoded against
    ;    Realtek's bit table once this raw value is in hand, not guessed at
    ;    here. ──
    mov esi, tcr_label
    call print32
    mov ebp, [mmio_base]
    mov eax, [ebp + 0x40]
    call print_hex32
    call emit_newline

    mov esi, rcr_label
    call print32
    mov ebp, [mmio_base]
    mov eax, [ebp + 0x44]
    call print_hex32
    call emit_newline

    ; ── Cfg9346 (0x50), Config1-5 (0x52-0x56), 1 byte each ──
    mov esi, cfg9346_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, byte [ebp + 0x50]
    call print_hex8
    call emit_newline

    mov esi, config1_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, byte [ebp + 0x52]
    call print_hex8
    mov al, ' '
    call emit_char
    movzx eax, byte [ebp + 0x53]
    call print_hex8
    mov al, ' '
    call emit_char
    movzx eax, byte [ebp + 0x54]
    call print_hex8
    mov al, ' '
    call emit_char
    movzx eax, byte [ebp + 0x55]
    call print_hex8
    mov al, ' '
    call emit_char
    movzx eax, byte [ebp + 0x56]
    call print_hex8
    call emit_newline

    ; ── PHYAR (0x60), 4 bytes; PHYstatus (0x6C), 1 byte ──
    mov esi, phyar_label
    call print32
    mov ebp, [mmio_base]
    mov eax, [ebp + 0x60]
    call print_hex32
    call emit_newline

    mov esi, phystatus_label
    call print32
    mov ebp, [mmio_base]
    movzx eax, byte [ebp + 0x6C]
    call print_hex8
    call emit_newline

    popa
    ret

; ── Software reset: write CmdReset (0x10) to ChipCmd (0x37), then poll
; ChipCmd until that bit self-clears -- confirmed against the Linux r8169
; driver source (ChipCmdBits: CmdReset=0x10, CmdRxEnb=0x08, CmdTxEnb=0x04),
; the same bit across the whole RTL8139/8169/8168 product line. This is
; the one write this probe makes: a single, standard, universally-expected
; operation with a well-defined completion signal, not exploratory
; poking -- about as safe as a PCI write gets. Bounded poll (RESET_POLL_
; LIMIT iterations) so a chip that never clears the bit doesn't hang this
; forever; reports which happened either way.
; ---------------------------------------------------------------------------
RESET_POLL_LIMIT equ 1000000

do_reset:
    pusha

    mov esi, reset_issuing_msg
    call print32

    mov ebp, [mmio_base]
    mov byte [ebp + 0x37], 0x10  ; CmdReset

    mov ecx, RESET_POLL_LIMIT
.poll_loop:
    mov ebp, [mmio_base]
    movzx eax, byte [ebp + 0x37]
    test al, 0x10
    jz .reset_done
    loop .poll_loop

    mov esi, reset_timeout_msg
    call print32
    jmp .end

.reset_done:
    mov esi, reset_done_msg
    call print32
    mov eax, RESET_POLL_LIMIT
    sub eax, ecx
    call print_hex32
    mov esi, reset_iters_suffix
    call print32

.end:
    popa
    ret

; =============================================================================
; TX_TEST — build one broadcast Ethernet frame, hand it to a single TX
; descriptor, ring the doorbell (TxPoll=0x38, NPQ=0x40, byte write -- both
; confirmed against the Linux r8169 driver source, not guessed), and poll
; the descriptor's OWN bit for hardware to actually consume it.
;
; Frame: dst=broadcast (FF:FF:FF:FF:FF:FF -- always safe, no host acts on
; an EtherType it doesn't recognize, switches just flood it like any other
; broadcast), src=this NIC's real MAC (read live from IDR0-5, not
; hardcoded), EtherType=0x88B5 (IEEE-reserved for local experimental use --
; deliberately not a real protocol number, so it can't be mistaken for
; anything and is trivially filterable in Wireshark: eth.type == 0x88b5),
; payload="HDGL-PROBE-HELLO" zero-padded to the 46-byte Ethernet minimum.
;
; Not certain whether this chip needs an explicit "insert FCS" descriptor
; bit the way e1000 does (CMD_IFCS) -- none is set here on the assumption
; C+ mode appends FCS unconditionally like normal Ethernet MACs generally
; do. If Wireshark flags a bad checksum on the captured frame, that
; assumption is the first thing to revisit.
;
; TX descriptor and frame buffer live at fixed, page-aligned scratch
; addresses (0x30000/0x31000), clear of the log buffer (0x20000-0x28000)
; and the stage2 image itself.
; =============================================================================
TX_DESC_ADDR  equ 0x30000
TX_BUF_ADDR   equ 0x31000
TX_POLL_LIMIT equ 1000000
FRAME_LEN     equ 14 + 46      ; header + Ethernet-minimum payload, excl. FCS

; =============================================================================
; DISABLE_RXDVGATE — rounds 4-6 all set up RX correctly (ring, buffer size,
; enable bit) and never received a single frame, two real-hardware test
; sends confirmed. Read against the Linux r8169 driver source: MISC
; (offset 0xF0) has an RXDV_GATED_EN bit (bit 19, 0x80000) that gates the
; RX-data-valid signal itself at the PHY/MAC boundary -- when set, no
; frame ever reaches the descriptor ring no matter how correctly it's
; configured. rtl_disable_rxdvgate() (clear this bit) is called early in
; essentially every RTL8168 hardware-start path in the real driver, not
; just the Wake-on-LAN path -- something this firmware never did at all.
; Called once, right after reset, before any TX/RX setup, matching where
; the real driver places it.
; =============================================================================
disable_rxdvgate:
    pusha

    mov esi, rxdvgate_msg
    call print32

    mov ebp, [mmio_base]
    mov eax, [ebp + 0xF0]
    and eax, 0xFFF7FFFF        ; clear bit 19 (RXDV_GATED_EN)
    mov [ebp + 0xF0], eax

    popa
    ret

tx_test:
    pusha

    mov esi, tx_test_hdr
    call print32

    ; ── Build the frame at TX_BUF_ADDR ──
    mov edi, TX_BUF_ADDR

    ; Dst MAC: broadcast
    mov dword [edi+0], 0xFFFFFFFF
    mov word  [edi+4], 0xFFFF

    ; Src MAC: read live from IDR0-5 (mmio_base+0..5), not hardcoded
    mov ebp, [mmio_base]
    movzx eax, byte [ebp+0]
    mov [edi+6], al
    movzx eax, byte [ebp+1]
    mov [edi+7], al
    movzx eax, byte [ebp+2]
    mov [edi+8], al
    movzx eax, byte [ebp+3]
    mov [edi+9], al
    movzx eax, byte [ebp+4]
    mov [edi+10], al
    movzx eax, byte [ebp+5]
    mov [edi+11], al

    ; EtherType 0x88B5, big-endian on the wire
    mov byte [edi+12], 0x88
    mov byte [edi+13], 0xB5

    ; Payload: "HDGL-PROBE-HELLO" (excluding its own null terminator, which
    ; stays in place for the later print32 of the same string), then
    ; zero-pad the rest of the buffer up to the 46-byte minimum.
    mov esi, tx_payload_str
    mov ecx, tx_payload_len
    lea edi, [TX_BUF_ADDR + 14]
    rep movsb
    mov eax, TX_BUF_ADDR + 14 + 46
    sub eax, edi
    jle .no_pad
    mov ecx, eax
    xor al, al
    rep stosb
.no_pad:

    ; ── Print what was built, so it's cross-checkable against a capture ──
    mov esi, tx_src_label
    call print32
    mov ebp, [mmio_base]
    xor edi, edi
.print_mac_loop:
    movzx eax, byte [ebp+edi]
    call print_hex8
    inc edi
    cmp edi, 6
    jge .print_mac_done
    mov al, ':'
    call emit_char
    jmp .print_mac_loop
.print_mac_done:
    call emit_newline

    mov esi, tx_ethertype_label
    call print32
    ; Print the two EtherType bytes individually in wire order (0x88 then
    ; 0xB5) -- a native word load here would read them little-endian and
    ; byte-swap the displayed value, even though the bytes actually in the
    ; frame buffer (and therefore actually on the wire) are correct.
    movzx eax, byte [TX_BUF_ADDR+12]
    call print_hex8
    movzx eax, byte [TX_BUF_ADDR+13]
    call print_hex8
    call emit_newline

    mov esi, tx_payload_label
    call print32
    mov esi, tx_payload_str
    call print32
    call emit_newline

    ; ── Fill the TX descriptor ──
    mov edi, TX_DESC_ADDR
    mov eax, 0x80000000            ; DescOwn
    or  eax, 0x40000000            ; RingEnd (our only descriptor)
    or  eax, 0x20000000            ; FirstFrag
    or  eax, 0x10000000            ; LastFrag
    or  eax, FRAME_LEN
    mov [edi], eax                 ; opts1
    mov dword [edi+4], 0           ; opts2 (no VLAN)
    mov dword [edi+8], TX_BUF_ADDR ; addr low
    mov dword [edi+12], 0          ; addr high

    ; ── Point the chip at the ring ──
    mov ebp, [mmio_base]
    mov dword [ebp+0x20], TX_DESC_ADDR   ; TxDescStartAddrLow
    mov dword [ebp+0x24], 0              ; TxDescStartAddrHigh

    ; ── Enable TX (ChipCmd |= CmdTxEnb) ──
    movzx eax, byte [ebp+0x37]
    or al, 0x04
    mov [ebp+0x37], al

    ; ── Ring the doorbell: TxPoll(0x38) = NPQ(0x40), byte write ──
    mov byte [ebp+0x38], 0x40

    mov esi, tx_ringing_msg
    call print32

    ; ── Poll the descriptor's OWN bit until hardware clears it ──
    mov ecx, TX_POLL_LIMIT
.poll_loop:
    mov eax, [TX_DESC_ADDR]
    test eax, 0x80000000
    jz .tx_done
    loop .poll_loop

    mov esi, tx_timeout_msg
    call print32
    jmp .end

.tx_done:
    mov esi, tx_done_msg
    call print32
    mov eax, TX_POLL_LIMIT
    sub eax, ecx
    call print_hex32
    mov esi, reset_iters_suffix
    call print32

.end:
    popa
    ret

; =============================================================================
; RX_ECHO_TEST — set up a single RX descriptor, enable the receiver, wait
; (bounded, but long -- this is waiting on a human sending a packet, not on
; the chip itself) for one incoming frame, log what arrived, then echo it
; straight back using the exact same TX mechanism tx_test already proved
; works: dst=broadcast, src=our real MAC, EtherType 0x88B5, payload=
; "ECHO:"+whatever text arrived. This is the two-way proof: a real frame
; sent FROM Windows, received by the bare-metal box, and replied to.
;
; RCR is left exactly as it already was (0x0002CF0E, confirmed real on
; probe rounds 2-3, already includes APM/AM/AB -- accepts our own unicast,
; multicast, and broadcast) -- not overwritten, since it already looked
; sane and changing it isn't needed here.
;
; RX descriptor/buffer live at fixed, page-aligned scratch addresses
; (0x32000/0x33000), clear of the TX descriptor/buffer (0x30000/0x31000) --
; RX_BUF_ADDR now needs a genuine 16KB of free space behind it (see below),
; which the next allocated region (none yet) doesn't encroach on.
;
; RX_DESC_BUF_SIZE / RX_MAX_SIZE: round 5's real-hardware test set both of
; these to 1536 (a normal single-frame allocation) and got no RX activity
; at all -- two frames sent from Windows, neither ever landed. Read
; against the actual Linux r8169 driver source (not guessed): it allocates
; a full 16KB per RX descriptor and uses two DIFFERENT exact values, not
; one reused for both registers:
;   - the descriptor's own buffer-size field (opts1 low 14 bits, the max
;     that field can represent) = R8169_RX_BUF_SIZE = SZ_16K-1 = 0x3FFF
;   - the RxMaxSize REGISTER (0xDA) = R8169_RX_BUF_SIZE+1 = 0x4000
; Matching both exactly, rather than assuming "big enough for one Ethernet
; frame" was sufficient -- it evidently wasn't.
; =============================================================================
RX_DESC_ADDR       equ 0x32000
RX_BUF_ADDR        equ 0x33000
RX_DESC_BUF_SIZE   equ 0x3FFF   ; descriptor opts1 field value (14-bit max)
RX_MAX_SIZE        equ 0x4000   ; RxMaxSize register value

; =============================================================================
; REMOTE_LOOP — persistent command loop. Replaces the one-shot RX echo
; test: instead of receiving one frame and halting, this re-arms the RX
; descriptor and waits again after every command, forever. Wire protocol
; (after the standard 14-byte Ethernet header + our EtherType 0x88B5), all
; multi-byte fields little-endian (native x86, no byte-swap needed on
; either end of this link):
;
;   byte 0 = opcode
;     0x00 PING:    remainder = arbitrary bytes, echoed back after "ECHO:"
;                   (same wire shape rounds 4-7 already proved)
;     0x01 READ32:  bytes 1-4 = target physical address
;                   reply: byte0=0x81, bytes1-4=address (echoed),
;                          bytes5-8=the 32-bit value actually read
;     0x02 WRITE32: bytes 1-4 = address, bytes 5-8 = value to write
;                   reply: byte0=0x82, bytes1-4=address (echoed),
;                          bytes5-8=value read back AFTER the write --
;                          reporting what's actually there now, not just
;                          echoing what was sent
;     0x04 PCI_CONFIG_READ: byte1=bus, byte2=device, byte3=function,
;                   byte4=config register (byte offset, must be dword-
;                   aligned, e.g. 0x18 for BAR2) -- reuses this firmware's
;                   own pci_read32 (already proven correct: it's exactly
;                   how this project's own PCI scan found the real NIC and
;                   the real GPU's BAR0/BAR3 sizes originally). Added
;                   2026-08-23 specifically to find the GPU's real VRAM
;                   aperture base address (BAR2/BAR3 pair) before ever
;                   attempting to write real image content into VRAM --
;                   the emulator's own VRAM_BAR constant is a placeholder,
;                   never a confirmed real address, and writing blind
;                   without knowing where VRAM actually is would be
;                   reckless given this project's history.
;                   reply: byte0=0x84, byte1-4=bus/device/function/register
;                   (echoed), bytes5-8=the 32-bit config dword read
;     0x03 READ_BLOCK: bytes 1-4 = start address, bytes 5-6 = dword count
;                   (16-bit, clamped to MAX_BLOCK_COUNT server-side --
;                   caller can tell a clamp happened by checking the
;                   echoed count)
;                   reply: byte0=0x83, bytes1-4=address (echoed),
;                          bytes5-6=count (echoed, post-clamp),
;                          bytes7..7+4*count-1 = that many consecutive
;                          32-bit values, little-endian, read via one
;                          `rep movsd` burst -- added 2026-08-23 because
;                          one-dword-per-round-trip was the actual
;                          bottleneck for any dense/graphical read of the
;                          GPU's address space, not the analysis method.
;                          ALL-OR-NOTHING: unlike READ32, if any address in
;                          the requested span wedges the bus, nothing comes
;                          back at all -- no partial results, same as a
;                          single bad READ32 but now with a whole block's
;                          worth of blast radius. Firmware does NOT know
;                          about this project's software hazard-range list
;                          (that guard lives in remote_control.py /
;                          fast_sweep.py on the Windows side, same place
;                          the WRITE32 vetting discipline already lives) --
;                          whoever calls this from Windows is responsible
;                          for not requesting a span that crosses a known-
;                          bad range.
;
; WRITE32 is NOT restricted to any address range in firmware -- this is a
; general-purpose remote memory access agent. Responsibility for only
; targeting addresses already vetted safe belongs to whoever drives it
; from the Windows side, the same discipline already established for the
; NVS295 GPU project's own write-testing (PFIFO/PDISPLAY0 safe-list).
;
; This loop never returns in the normal case -- once started, the boot
; image stays a persistent, network-controllable agent. The on-disk log
; is not used from this point on (it only ever flushes once, at halt,
; which this loop deliberately never reaches); the real-time feedback
; channel is the network reply itself, plus whatever's visible on-screen.
; =============================================================================
remote_loop:
    pusha

    mov esi, remote_loop_hdr
    call print32

.next_command:
    ; ── Re-arm the RX descriptor: hand hardware a fresh empty buffer ──
    mov edi, RX_DESC_ADDR
    mov eax, 0x80000000
    or  eax, 0x40000000
    or  eax, RX_DESC_BUF_SIZE
    mov [edi], eax
    mov dword [edi+4], 0
    mov dword [edi+8], RX_BUF_ADDR
    mov dword [edi+12], 0

    mov ebp, [mmio_base]
    mov dword [ebp+0xE4], RX_DESC_ADDR
    mov dword [ebp+0xE8], 0
    mov word  [ebp+0xDA], RX_MAX_SIZE
    movzx eax, byte [ebp+0x37]
    or al, 0x08
    mov [ebp+0x37], al

.wait_loop:
    mov eax, [RX_DESC_ADDR]
    test eax, 0x80000000
    jnz .wait_loop

    movzx eax, byte [RX_BUF_ADDR + 14]   ; opcode
    cmp al, 0x01
    je .do_read32
    cmp al, 0x02
    je .do_write32
    cmp al, 0x03
    je .do_read_block
    cmp al, 0x04
    je .do_pci_read

    mov esi, remote_got_msg
    call print32
    call emit_newline
    call remote_send_ping_reply
    jmp .next_command

.do_read32:
    mov eax, [RX_BUF_ADDR + 15]      ; target address, little-endian
    mov [remote_addr], eax
    mov ebp, eax
    mov eax, [ebp]                    ; the actual read
    mov [remote_value], eax

    mov esi, remote_read_msg
    call print32
    mov eax, [remote_addr]
    call print_hex32
    mov esi, remote_eq_sp
    call print32
    mov eax, [remote_value]
    call print_hex32
    call emit_newline

    call remote_send_read_reply
    jmp .next_command

.do_write32:
    mov eax, [RX_BUF_ADDR + 15]      ; address
    mov [remote_addr], eax
    mov ebp, eax
    mov edx, [RX_BUF_ADDR + 19]      ; value
    mov [ebp], edx                    ; the actual write
    mov eax, [ebp]                    ; read back -- report what's really
                                       ; there now, not just what was sent
    mov [remote_value], eax

    mov esi, remote_write_msg
    call print32
    mov eax, [remote_addr]
    call print_hex32
    mov esi, remote_eq_sp
    call print32
    mov eax, [remote_value]
    call print_hex32
    call emit_newline

    call remote_send_write_reply
    jmp .next_command

.do_read_block:
    mov eax, [RX_BUF_ADDR + 15]          ; start address, little-endian
    mov [remote_addr], eax
    movzx eax, word [RX_BUF_ADDR + 19]   ; requested count, little-endian
    cmp eax, MAX_BLOCK_COUNT
    jbe .rb_count_ok
    mov eax, MAX_BLOCK_COUNT             ; clamp -- echoed count tells the caller
.rb_count_ok:
    mov [remote_count], eax

    mov esi, remote_readblock_msg
    call print32
    mov eax, [remote_addr]
    call print_hex32
    mov esi, remote_count_label
    call print32
    mov eax, [remote_count]
    call print_hex32
    call emit_newline

    call remote_send_readblock_reply
    jmp .next_command

.do_pci_read:
    movzx eax, byte [RX_BUF_ADDR + 15]
    mov [remote_pci_bus], eax
    movzx eax, byte [RX_BUF_ADDR + 16]
    mov [remote_pci_dev], eax
    movzx eax, byte [RX_BUF_ADDR + 17]
    mov [remote_pci_func], eax
    movzx eax, byte [RX_BUF_ADDR + 18]
    mov [remote_pci_reg], eax

    mov ebx, [remote_pci_bus]
    mov ecx, [remote_pci_dev]
    mov edx, [remote_pci_func]
    mov esi, [remote_pci_reg]
    call pci_read32
    mov [remote_pci_value], eax

    mov esi, remote_pciread_msg
    call print32
    mov eax, [remote_pci_value]
    call print_hex32
    call emit_newline

    call remote_send_pciread_reply
    jmp .next_command

remote_addr:  dd 0
remote_value: dd 0
remote_count: dd 0
remote_frame_len: dd 0
remote_pci_bus: dd 0
remote_pci_dev: dd 0
remote_pci_func: dd 0
remote_pci_reg: dd 0
remote_pci_value: dd 0
MAX_BLOCK_COUNT equ 256   ; 256 dwords = 1024 data bytes; total frame
                          ; (14 hdr + 1 op + 4 addr + 2 count + 1024 data)
                          ; = 1045 bytes, comfortably under the 1500-byte
                          ; standard Ethernet MTU

; Fills TX_BUF_ADDR[0..13] with dst=broadcast/src=our real MAC (read live
; from IDR0-5)/EtherType=0x88B5. Caller fills the payload afterward.
remote_build_eth_header:
    pusha
    mov edi, TX_BUF_ADDR
    mov dword [edi+0], 0xFFFFFFFF
    mov word  [edi+4], 0xFFFF
    mov ebp, [mmio_base]
    movzx eax, byte [ebp+0]
    mov [edi+6], al
    movzx eax, byte [ebp+1]
    mov [edi+7], al
    movzx eax, byte [ebp+2]
    mov [edi+8], al
    movzx eax, byte [ebp+3]
    mov [edi+9], al
    movzx eax, byte [ebp+4]
    mov [edi+10], al
    movzx eax, byte [ebp+5]
    mov [edi+11], al
    mov byte [edi+12], 0x88
    mov byte [edi+13], 0xB5
    popa
    ret

; Transmits whatever's currently sitting in TX_BUF_ADDR[0..FRAME_LEN-1] --
; fill descriptor, ring the doorbell, poll for completion (bounded,
; already proven fast/instant on this real chip).
remote_send_frame:
    pusha
    mov edi, TX_DESC_ADDR
    mov eax, 0x80000000
    or  eax, 0x40000000
    or  eax, 0x20000000
    or  eax, 0x10000000
    or  eax, FRAME_LEN
    mov [edi], eax
    mov dword [edi+4], 0
    mov dword [edi+8], TX_BUF_ADDR
    mov dword [edi+12], 0

    mov ebp, [mmio_base]
    mov dword [ebp+0x20], TX_DESC_ADDR
    mov dword [ebp+0x24], 0

    movzx eax, byte [ebp+0x37]
    or al, 0x04
    mov [ebp+0x37], al

    mov byte [ebp+0x38], 0x40

    mov ecx, TX_POLL_LIMIT
.poll_loop:
    mov eax, [TX_DESC_ADDR]
    test eax, 0x80000000
    jz .done
    loop .poll_loop
.done:
    popa
    ret

; Same as remote_send_frame but uses [remote_frame_len] (set by the caller)
; instead of the fixed FRAME_LEN constant -- needed because READ_BLOCK
; replies vary in size with the requested count, unlike every other reply
; this agent sends.
remote_send_frame_var:
    pusha
    mov edi, TX_DESC_ADDR
    mov eax, 0x80000000
    or  eax, 0x40000000
    or  eax, 0x20000000
    or  eax, 0x10000000
    or  eax, [remote_frame_len]
    mov [edi], eax
    mov dword [edi+4], 0
    mov dword [edi+8], TX_BUF_ADDR
    mov dword [edi+12], 0

    mov ebp, [mmio_base]
    mov dword [ebp+0x20], TX_DESC_ADDR
    mov dword [ebp+0x24], 0

    movzx eax, byte [ebp+0x37]
    or al, 0x04
    mov [ebp+0x37], al

    mov byte [ebp+0x38], 0x40

    mov ecx, TX_POLL_LIMIT
.poll_loop:
    mov eax, [TX_DESC_ADDR]
    test eax, 0x80000000
    jz .done
    loop .poll_loop
.done:
    popa
    ret

remote_send_ping_reply:
    pusha
    call remote_build_eth_header

    mov byte [TX_BUF_ADDR+14], 0x80

    mov esi, rx_echo_prefix
    mov ecx, rx_echo_prefix_len
    lea edi, [TX_BUF_ADDR + 15]
    rep movsb

    mov esi, RX_BUF_ADDR + 15
    mov ecx, 39
    rep movsb

    mov eax, TX_BUF_ADDR + 14 + 46
    sub eax, edi
    jle .no_pad
    mov ecx, eax
    xor al, al
    rep stosb
.no_pad:
    call remote_send_frame
    popa
    ret

remote_send_read_reply:
    pusha
    call remote_build_eth_header
    mov byte [TX_BUF_ADDR+14], 0x81
    mov eax, [remote_addr]
    mov [TX_BUF_ADDR+15], eax
    mov eax, [remote_value]
    mov [TX_BUF_ADDR+19], eax

    mov edi, TX_BUF_ADDR+23
    mov eax, TX_BUF_ADDR + 14 + 46
    sub eax, edi
    mov ecx, eax
    xor al, al
    rep stosb

    call remote_send_frame
    popa
    ret

remote_send_write_reply:
    pusha
    call remote_build_eth_header
    mov byte [TX_BUF_ADDR+14], 0x82
    mov eax, [remote_addr]
    mov [TX_BUF_ADDR+15], eax
    mov eax, [remote_value]
    mov [TX_BUF_ADDR+19], eax

    mov edi, TX_BUF_ADDR+23
    mov eax, TX_BUF_ADDR + 14 + 46
    sub eax, edi
    mov ecx, eax
    xor al, al
    rep stosb

    call remote_send_frame
    popa
    ret

; Bulk reply: copies [remote_count] consecutive dwords starting at physical
; address [remote_addr] straight into the TX buffer with one `rep movsd`
; (this is the entire throughput win -- one descriptor/doorbell/poll cycle
; for up to 256 dwords instead of 256 of them), then pads to the Ethernet
; minimum only if the actual payload is smaller than that.
remote_send_readblock_reply:
    pusha
    call remote_build_eth_header
    mov byte [TX_BUF_ADDR+14], 0x83
    mov eax, [remote_addr]
    mov [TX_BUF_ADDR+15], eax
    mov ax, [remote_count]
    mov [TX_BUF_ADDR+19], ax

    mov esi, [remote_addr]
    mov edi, TX_BUF_ADDR + 21
    mov ecx, [remote_count]
    rep movsd

    ; frame length (14-byte header + 1 opcode + 4 addr + 2 count + 4*count
    ; data), padded up to the 60-byte Ethernet minimum if smaller
    mov eax, [remote_count]
    shl eax, 2
    add eax, 21
    cmp eax, 60
    jae .rb_len_set
    mov edi, TX_BUF_ADDR
    add edi, eax
    mov ecx, 60
    sub ecx, eax
    push eax
    xor al, al
    rep stosb
    pop eax
    mov eax, 60
.rb_len_set:
    mov [remote_frame_len], eax

    call remote_send_frame_var
    popa
    ret

remote_send_pciread_reply:
    pusha
    call remote_build_eth_header
    mov byte [TX_BUF_ADDR+14], 0x84
    mov al, [remote_pci_bus]
    mov [TX_BUF_ADDR+15], al
    mov al, [remote_pci_dev]
    mov [TX_BUF_ADDR+16], al
    mov al, [remote_pci_func]
    mov [TX_BUF_ADDR+17], al
    mov al, [remote_pci_reg]
    mov [TX_BUF_ADDR+18], al
    mov eax, [remote_pci_value]
    mov [TX_BUF_ADDR+19], eax

    mov edi, TX_BUF_ADDR+23
    mov eax, TX_BUF_ADDR + 14 + 46
    sub eax, edi
    mov ecx, eax
    xor al, al
    rep stosb

    call remote_send_frame
    popa
    ret

nic_dump_exit:
    pop edx
    pop ecx
    pop ebx
    popa
    ret

bar2_raw:   dd 0
bar3_raw:   dd 0
mmio_base:  dd 0

; -----------------------------------------------------------------------------
; Strings
; -----------------------------------------------------------------------------
pm_banner:
    db 'REMOTE_AGENT -- persistent network-controlled agent (RTL8168)', 10
    db '===============================================================', 10, 10, 0
scanning_msg:  db 'Scanning PCI for the target NIC (vendor:device overridable via -D)...', 10, 0
not_found_msg: db 10, 'Target NIC NOT FOUND on any bus.', 10, 0
found_msg:     db 10, '>>> TARGET NIC FOUND <<<', 10, 0

bar2_label:        db '  BAR2 (raw)      = 0x', 0
bar3_label:        db '  BAR3 (raw)      = 0x', 0
bar3_nonzero_msg:  db '  BAR3 is non-zero -- MMIO base is above 4GB, unreachable from this flat 32-bit environment. Stopping.', 10, 0
mmio_base_label:   db '  MMIO base       = 0x', 0

mac_label:         db 'MAC (IDR0-5)         = ', 0
cr_label:          db 'CR       @0x37 (1B)  = 0x', 0
imr_label:         db 'IMR      @0x3C (2B)  = 0x', 0
isr_label:         db 'ISR      @0x3E (2B)  = 0x', 0
tcr_label:         db 'TCR      @0x40 (4B)  = 0x', 0
rcr_label:         db 'RCR      @0x44 (4B)  = 0x', 0
cfg9346_label:      db 'Cfg9346  @0x50 (1B)  = 0x', 0
config1_label:      db 'Config1-5 @0x52-56   = 0x', 0
phyar_label:        db 'PHYAR    @0x60 (4B)  = 0x', 0
phystatus_label:    db 'PHYstatus@0x6C (1B)  = 0x', 0

before_reset_hdr:   db 10, '-- BEFORE RESET --', 10, 0
rxdvgate_msg:       db 10, 'Clearing RXDV_GATED_EN (MISC bit 19 @0xF0) -- unblocks RX at the PHY/MAC boundary...', 10, 0
after_reset_hdr:     db 10, '-- AFTER RESET --', 10, 0
reset_issuing_msg:   db 10, 'Issuing software reset (writing CmdReset=0x10 to ChipCmd @0x37)...', 10, 0
reset_done_msg:       db '  Reset completed. Poll iterations: 0x', 0
reset_iters_suffix:   db 10, 0
reset_timeout_msg:    db '  Reset TIMED OUT -- CmdReset bit never cleared within the poll limit.', 10, 0

tx_test_hdr:        db 10, '-- TX TEST (one broadcast frame, EtherType 0x88B5) --', 10, 0
tx_src_label:        db '  src MAC        = ', 0
tx_ethertype_label:  db '  EtherType      = 0x', 0
tx_payload_label:    db '  payload        = ', 0
tx_ringing_msg:      db '  Ringing TX doorbell (TxPoll @0x38 = NPQ 0x40)...', 10, 0
tx_done_msg:         db '  TX descriptor consumed by hardware. Poll iterations: 0x', 0
tx_timeout_msg:      db '  TX TIMED OUT -- descriptor OWN bit never cleared within the poll limit.', 10, 0

tx_payload_str: db 'HDGL-PROBE-HELLO', 0
tx_payload_len equ ($ - tx_payload_str) - 1

remote_loop_hdr: db 10, '-- REMOTE COMMAND LOOP (persistent; PING/READ32/WRITE32) --', 10, 0
remote_got_msg:  db '  Command received: ', 0
remote_read_msg:  db '  READ32  0x', 0
remote_write_msg: db '  WRITE32 0x', 0
remote_eq_sp:     db ' = 0x', 0
remote_readblock_msg: db '  READBLK 0x', 0
remote_count_label:   db ' count=0x', 0
remote_pciread_msg:   db '  PCIREAD = 0x', 0

rx_echo_prefix: db 'ECHO:', 0
rx_echo_prefix_len equ ($ - rx_echo_prefix) - 1

times 16384-($-$$) db 0
