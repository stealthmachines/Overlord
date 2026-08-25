; =============================================================================
; STAGE 1 (PXE variant) — replaces the disk-based boot_15.asm's job when this
; image is delivered as a legacy PXE network boot program instead of from the
; USB stick. Legacy PXE loads the WHOLE served file as one contiguous blob at
; 0x0000:0x7C00 and jumps to offset 0 -- there is no subsequent disk read the
; way a real BIOS MBR gets one; stage2 is already sitting in RAM by the time
; this code runs, delivered by TFTP as part of the same file. So this stub
; does everything boot_15.asm does EXCEPT the INT13 LBA read -- it must NOT
; touch disk at all here, since (a) it isn't needed, the bytes are already in
; place, and (b) issuing our own INT13 read using DL as PXE happens to leave
; it risks overwriting the just-delivered stage2 with stale disk content from
; whatever's on the boot media, if any is even present.
;
; File layout this expects (built by the PXE packaging step, not by nasm
; directly): [this 512-byte sector][512 bytes zero padding][stage2_15.bin].
; The 512-byte pad exists purely so stage2 lands at physical 0x8000 -- the
; address its own ORG assumes -- exactly matching where boot_15.asm's INT13
; read would have placed it, so stage2_15.bin itself needs zero changes to
; work identically regardless of which stage1 delivered it.
; =============================================================================

BITS 16
ORG 0x7C00

start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7C00
    sti

    call serial_init_16
    mov al, 'A'                 ; DEBUG: stage1 (PXE) alive
    call serial_putchar_16

    ; No real boot drive in a PXE context -- this project's own probing
    ; already established the USB stick this same image also lives on is
    ; BIOS drive 0x00 (confirmed via GET_BOOT_DRIVE + a read-only LBA0
    ; sanity read matching this exact image byte-for-byte, round 14). Fix
    ; it at that known-good value so self_flash() and disk_write_sectors()
    ; still have a valid target after a PXE-delivered boot.
    mov byte [0x7000], 0x00

    mov si, msg_loading
    call print_str16

    mov al, '>'                 ; DEBUG: jumping straight into stage2 --
    call serial_putchar_16      ; already resident, no disk read needed

    jmp 0x0000:0x8000

; Minimal BIOS-teletype string printer (real mode only).
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

; Minimal 16-bit COM1 debug output -- same UART, initialized independently
; here so stage1's progress is visible even if stage2 never runs at all.
serial_init_16:
    push ax
    push dx
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 0x80
    out dx, al
    mov dx, 0x3F8
    mov al, 3
    out dx, al
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 0x03
    out dx, al
    mov dx, 0x3FA
    mov al, 0xC7
    out dx, al
    mov dx, 0x3FC
    mov al, 0x0B
    out dx, al
    pop dx
    pop ax
    ret

serial_putchar_16:              ; AL = char
    push ax
    push bx
    push dx
    mov bl, al
.wait:
    mov dx, 0x3FD
    in al, dx
    test al, 0x20
    jz .wait
    mov dx, 0x3F8
    mov al, bl
    out dx, al
    pop dx
    pop bx
    pop ax
    ret

msg_loading: db 'REMOTE_AGENT (PXE): stage2 already resident, jumping...', 13, 10, 0

times 510-($-$$) db 0
dw 0xAA55
